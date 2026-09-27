# OPUS55 PK-8 Implementation Report: placing an exit is not exiting (P0-7)

| | |
|---|---|
| Packet | PK-8 only. It fixes **P0-7**. No P1 packet was started, and PK-13 (the partial-fill model) was not started. |
| Baseline before | 432 passed, 2 xfailed (P0-7 ×2), 0 failed, 0 xpassed. Re-measured before any edit. |
| Result after | **455 passed, 0 xfailed, 0 failed, 0 xpassed** (3 identical full runs on the final code). Every P0 regression test now passes normally. |
| Backups taken first | Tag `backup-pre-pk8` and branch `backup/pre-pk8`, both at `c27f0fe`. Tag `backup-pre-pk8-worktree` (commit `827b96a`) is a whole-working-tree snapshot, including the uncommitted PK-0 to PK-7 files and untracked files. It was built through a temporary index, so the real index, HEAD and working tree were not touched. `backend/kite_terminal.db.pre_pk8_bak` was taken with the SQLite online-backup API; `integrity_check` = ok. |
| Schema | Unchanged. No new column, no new status value, no new `ticket_events` type, no index change and no migration. `backend/db.py` is byte-identical to the pre-PK-8 snapshot. |
| Committed | No. Nothing was committed, as instructed. |

## 1. Executive summary

Before PK-8, a live explicit exit counted as done the moment Kite returned an order id. This applied to a strategy exit, a Quick-Action sell linked to a position, and an Emergency Exit. The confirm path did four things in the same request:

- deleted the position's protective GTT;
- stamped the position's `closed_at`;
- superseded the sibling exits;
- wrote the `closed_trades` row at the **reference** price.

An exit that was still OPEN, an AMO waiting overnight, a partial fill, or an order the exchange later rejected all left the position shown as closed, unprotected and journaled. A later rejection then relied on a best-effort "reopen" that could not restore the deleted GTT.

PK-8 introduces an explicit **EXITING** state and makes `order_monitor` the **only** component that closes a live explicit exit's position.

- **After Kite accepts the exit order:**
  - the exit is EXITING;
  - the position stays **open**, `closed_at` stays NULL, and the GTT stays **active**;
  - nothing is journaled;
  - the position still counts for max open positions, portfolio risk, Emergency Exit and reconciliation;
  - any sibling exit is refused with `EXIT_ALREADY_IN_PROGRESS`.
- **When Kite reports the full quantity COMPLETE with an average price,** the monitor, exactly once:
  - records the broker fill price and time;
  - stamps `closed_at` with that fill time (conditional update);
  - supersedes the siblings;
  - journals the trade at the fill price;
  - then removes the GTT.
- **Every other outcome keeps the position open:** OPEN, AMO, trigger pending, a partial fill, REJECTED, CANCELLED, UNKNOWN, and a COMPLETE without a usable fill figure.

Both P0-7 xfails now pass normally. There are 21 new tests, all 8 required mutations are caught, and all 455 tests pass.

## 2. Files changed

This is PK-8's own diff, measured against `backup-pre-pk8-worktree` (added/removed lines).

| File | ± | Change |
|---|---|---|
| `backend/execution.py` | +47/−49 | `_execute_exit` no longer deletes the GTT; its message says EXITING. `cancel_original_gtt` was removed. It adds `EXIT_FILL_PROVEN_SQL` and `exit_state()`, plus a lifecycle comment block. The `supersede_sibling_exits` docstring now says it runs only on a proven fill. |
| `backend/order_monitor.py` | +187/−53 | Adds `_finalize_exit`, the closure authority, and `_remove_protective_gtt` (ORPHANED GTT and POSSIBLE DOUBLE SELL handling). It adds `_flag_partial_exit_in_progress`, `_flag` (de-duplicated alert) and `_fill_time_utc`. `_resolve_exit` is rewritten and its reopen logic is removed. `_adopt` no longer deletes the GTT or closes the position. The module docstring is updated. |
| `backend/journal.py` | +40/−22 | `close_position_for_exit` now takes the fill time, is only called on a proven fill, uses a conditional stamp, and returns whether it closed the position. `record_closed_trade` refuses an unfilled exit and dates the trade by `filled_at`. `backfill_closed_trades` only picks up exits whose fill is proven. |
| `backend/routes/tickets.py` | +34/−22 | Confirm closes a position immediately **only** for a filled (dry-run) exit. It adds `exit_state` to every serialized ticket and `exit_in_flight_ticket_id` to the ticket list, via `_exits_in_flight`. |
| `backend/risk_engine.py` | +22/−8 | `_CLOSED_BY_EXECUTED_EXIT` now requires a proven fill. `linked_exit_problem` treats an EXITING or partial owner as `EXIT_ALREADY_IN_PROGRESS`, which is not permanent. |
| `backend/routes/kill_switch.py` | +41/−7 | Adds the `already_exiting` and `exit_submitted`/`closed`/`failed` result statuses and `_exit_in_flight`. The preview gains `exit_in_flight`. The system event now says "exit requested". |
| `backend/strategy_engine.py` | +5/−2 | `_has_pending_exit` counts EXITING and partial exits, so the scanner does not re-create an exit ticket every cycle. |
| `frontend/app.js` | +25/−7 | Adds the **EXITING · awaiting fill** badge, **PARTIAL EXIT — NEEDS REVIEW** badge and a position "exiting" chip, the EXITING approval toast, and the Emergency Exit toast. |
| `frontend/style.css` | +5 | Adds the `.status-badge.exiting` rule. |
| `tests/test_exit_lifecycle.py` | new, 474 lines | 21 PK-8 tests, cases A–N plus extras. |
| `tests/test_live_safety.py` | +16/−5 | P0-7 xfail marker removed; three tests restated for PK-8 semantics (section 20). |
| `tests/test_review_changes.py`, `tests/test_baseline_api.py`, `tests/test_gtt_watcher.py` | small | Tests that encoded "closed at order acceptance" now run a monitor cycle or assert EXITING first (section 20). |

The following were **not** changed:

- `backend/trailing_engine.py` (the PK-7 watcher);
- `backend/db.py`;
- `backend/reconciliation.py`;
- `tests/fake_kite.py`, since the existing FakeKite behaviours were sufficient;
- `docs/OPUS55_AUDIT_REPORT.md`.

## 3. Previous unsafe explicit-exit lifecycle

The flow was traced before any edit.

| | Before PK-8 |
|---|---|
| A. exit `order_id` stored | `tickets._record_execution_result`: status `executed`, `entry_fill_status='pending'`, `submission_state='acknowledged'`. |
| B. GTT deleted | `execution._execute_exit` → `cancel_original_gtt`, immediately after `place_order`. The same happened in `order_monitor._adopt` for a recovered UNKNOWN exit. |
| C. `closed_at` stamped | `journal.close_position_for_exit`, called from `confirm_and_execute_ticket` and `_adopt`. |
| D. journal row | Same call, at the **reference** price. `apply_exit_fill` re-priced the row on COMPLETE. `backfill_closed_trades` journaled **any** executed exit at every startup. |
| E. REJECTED/CANCELLED | `_resolve_exit` "reopened" the position. It nulled `closed_at`, and also `kite_gtt_id` because the GTT was already gone, deleted the journal row, and raised an UNPROTECTED alert. |
| F. monitor expectations | It followed `executed` + `pending` orders and acted on terminal statuses only; a partial OPEN fill was silently skipped. |
| G. Emergency Exit | It skipped only `confirmed` (UNKNOWN) exits and reported each accepted order as `executed`. |

## 4. New EXITING lifecycle

```
pending_confirmation
  → confirmed / submitting                (PK-1 claim, PK-2 tag)
  → confirmed / unknown                   (ambiguous broker failure; monitor searches by tag)
  → executed / acknowledged|adopted,
    entry_fill_status = 'pending'         ← EXITING: order at the broker, position OPEN, GTT active
       ├─ OPEN / AMO REQ RECEIVED / TRIGGER PENDING  → stays EXITING
       ├─ working with some fills                    → stays EXITING + urgent PARTIAL EXIT flag
       ├─ COMPLETE, full qty, avg > 0                → entry_fill_status 'filled'   ← POSITION CLOSED
       │                                               → closed_at = fill time → siblings superseded
       │                                               → journal (once) → protective GTT removed
       ├─ ended after partial fill                   → 'partial' (NEEDS REVIEW, still owns the position)
       └─ REJECTED / CANCELLED, 0 filled             → status 'failed' (position untouched, GTT kept)
```

This is the smallest state change that works. EXITING is the existing combination `status='executed'` + `entry_fill_status='pending'` on a closing sell; PK-8 changes what that combination *means*. `execution.exit_state()` names it for the API and UI:

| `exit_state` | Meaning |
|---|---|
| `exiting` | EXITING |
| `exit_partial` | partial fill; needs review |
| `exit_unconfirmed` | needs review |
| `exited` | filled |

## 5. When a position is considered open vs closed

A live position is **closed** only when its `closed_at` is set. For an explicit exit, only `journal.close_position_for_exit` sets it, and that is called in exactly two places:

- from `order_monitor._finalize_exit`, after a broker COMPLETE for the full quantity with an average price;
- from `confirm_and_execute_ticket` for an exit that is already `filled`, which only a dry-run simulation can be.

`risk_engine._CLOSED_BY_EXECUTED_EXIT`, the fallback for rows from before fills were tracked, now also requires `COALESCE(entry_fill_status, 'filled') = 'filled'`. An EXITING, partial or unconfirmed exit therefore never hides its position.

## 6. GTT retention and cancellation policy

- **Placement.** Nothing is touched. The GTT remains the position's only protection until the fill.
- **UNKNOWN, adoption, OPEN, AMO, partial, REJECTED or CANCELLED.** Nothing is touched.
- **After a proven full fill,** `_remove_protective_gtt` reads the GTT's state from `get_gtts()`:
  - **active, or the book is unreadable:** it calls `delete_gtt` and nulls the position's `kite_gtt_id`.
  - **the delete fails:** the position **stays closed**, because the shares are sold. It raises an urgent **ORPHANED GTT** alert ("delete it in Kite NOW"), logs a `reconciliation_log` row and a position event, and keeps `kite_gtt_id` as evidence.
  - **already `triggered`:** the GTT may have sold a second time. It raises an urgent **POSSIBLE DOUBLE SELL** alert and logs a mismatch; nothing is deleted and nothing is sent.
  - **deleted, cancelled, expired or missing:** it adds a note only.
- The GTT is removed *after* the closure is committed. If the process dies in between, the result is an orphaned GTT against a correctly closed position, never an unprotected open one. Orphan reverse-reconciliation is P1 (section 26).

## 7. Order-monitor finalization

`_finalize_exit` works through these steps:

1. **Claim the fill.** A compare-and-set `pending → filled` on the exit (`_claim_pending`) records `filled_price = average_price` and `filled_at` = the order's `exchange_timestamp`, converted from IST to UTC; if that is missing it falls back to `order_timestamp`, then to now. This claim is what makes finalization happen exactly once across cycles and processes.
2. **Close the position.** In the **same transaction**, `journal.close_position_for_exit` runs:
   - `UPDATE … SET closed_at = <fill time> WHERE id = ? AND closed_at IS NULL`, checking the rowcount;
   - `supersede_sibling_exits`;
   - commit;
   - `record_closed_trade`, which is idempotent and backed by the unique `idx_closed_trades_sell_ticket`. A row that already exists from before PK-8 is re-priced at the fill.
3. **Log.** A single `closed` event goes on the position.
4. **Remove the GTT.** `_remove_protective_gtt` runs (section 6).
5. **Alert.** The broadcast is urgent if step 4 raised an ORPHANED GTT or DOUBLE SELL alert, otherwise success.

A COMPLETE order that reports fewer shares than the exit, or no average price, is **not** finalized. It stays EXITING and is flagged once. The reference price is never used.

## 8. OPEN / AMO behaviour

The pending loop leaves a non-terminal order alone, whether it is OPEN, `AMO REQ RECEIVED` or `TRIGGER PENDING`: the exit stays EXITING, with no `closed_at`, no journal row and the GTT active.

- An AMO placed after hours is followed across later cycles (test B pins the monitor clock through the evening and night) and closes on the next morning's fill. `closed_at` and the journal `exit_date` use the **fill** time.
- `record_closed_trade` now dates a trade by `filled_at` rather than the order's `executed_at`.
- A MARKET exit placed during market hours gets no special treatment; it still waits for COMPLETE.

## 9. COMPLETE behaviour

See section 7. Test C/M checks every effect on a single COMPLETE, and that 4 repeated monitor-plus-watcher cycles change nothing more:

- `closed_at` = fill time;
- the exit's `filled_price` and `filled_at`;
- one `closed_trades` row at 3120.5, with `exit_date` from the fill;
- the GTT deleted, with one `delete_gtt` call;
- one `closed` event;
- one SELL;
- open-position count 0 and open risk 0;
- `exit_state` `exited`.

## 10. REJECTED / CANCELLED behaviour

A zero-filled REJECTED or CANCELLED exit moves to `failed` with `entry_fill_status` set to `rejected` or `cancelled`. It no longer owns the position (PK-4 index), so a sibling exit may now close it.

**The obsolete reopen logic was removed**, because nothing was closed, deleted or journaled when the exit was placed:

- `closed_at = NULL`;
- `kite_gtt_id = NULL`;
- `DELETE FROM closed_trades`.

**Kept:**

- the urgent alert;
- the events on the exit and the position;
- the `reconciliation_log` row (expected "exit order filled");
- the GTT state check. It now only makes the alert accurate: "still active", or "NOT active at Kite — the GTT watcher follows it up".

## 11. PARTIAL behaviour

PK-13 will model this properly. For PK-8:

- **A working order with some fills:** the exit stays EXITING. An urgent **PARTIAL EXIT** alert goes out once per distinct filled quantity, via the `reconciliation_log` de-duplication, so it fires again as more shares fill.
- **An order that ended after a partial fill:**
  - the exit becomes `partial`;
  - it **stays `executed`, so it keeps owning the position**, blocking sibling exits and Emergency Exit (`already_exiting`);
  - nothing is closed or journaled, and the GTT is kept;
  - the urgent message says the position is not closed and the GTT still covers the full quantity, so it should be reduced in Kite;
  - **nothing is resubmitted**.
- **Limitation:** the position stays blocked until a human resolves it in Kite. No in-app "resolve partial" action exists; that is PK-13.

## 12. UNKNOWN recovery behaviour (PK-2)

An UNKNOWN exit is `confirmed`/`unknown`. The position stays open with its GTT, nothing is journaled, and siblings are blocked. It is never returned to `pending_confirmation`.

When the monitor adopts the tagged broker order, `_adopt` now only records the adoption. The exit becomes EXITING and the pending loop takes over **in the same cycle**, so an order that has already filled closes at once (test H2), while an OPEN one stays EXITING (G). The `not_found` and `needs_review` paths are unchanged.

## 13. PK-3 mode interaction

The exit's mode is still inherited from the position at creation or claim, never from the global switch.

- **Live exit:** the EXITING lifecycle above.
- **Dry-run exit:** the simulation returns `entry_fill_status='filled'`, so `confirm_and_execute_ticket` closes and journals it immediately through the same `close_position_for_exit`. There is no broker fill to wait for; this is internally consistent and makes no broker calls.

The dry-run close is gated on `entry_fill_status == 'filled'`, which a live acceptance never is, so no dry-run code path can finalize a live position. The PK-3 tests pass unchanged. The one exception is the "leaves dry-run positions alone" test, which now runs a monitor cycle before asserting that the **live** position closed.

## 14. PK-4 exit ownership and index interaction

**The index was not changed.** `idx_trade_tickets_one_exit_owner` covers `status IN ('confirmed', 'executed')`. Because EXITING and partial exits keep status `executed`, the storage-level invariant already prevents a second active exit of the same position. This is why no new status value was introduced (section 27).

The application check `linked_exit_problem`:

| Owning exit | Response | Permanent? |
|---|---|---|
| executed and fill proven | `EXIT_POSITION_CLOSED` | yes |
| EXITING or partial | `EXIT_ALREADY_IN_PROGRESS` ("awaiting its broker fill (EXITING)", or "partial — needs review") | no |
| confirmed (UNKNOWN) | unchanged | — |

Because the EXITING case is not permanent, the sibling stays `pending_confirmation` with no event written. It is superseded when the owner fills, or becomes approvable again if the owner is rejected (test J, both branches).

Supersede now happens only on a proven fill. It is never triggered by acceptance or adoption.

## 15. PK-7 GTT watcher interaction

`trailing_engine.py` is unchanged.

- **While EXITING,** the GTT stays active, so the watcher has nothing to judge for that position. Trailing may still ratchet the stop; this is harmless, and freezing it is deferred.
- **The GTT fires while the exit owns the position:** the existing `_handle_owned_closure` records **no** synthetic exit and raises **POSSIBLE DOUBLE SELL** once. From there:
  - **The explicit exit is later rejected** (for example because its shares were already sold). Ownership drops, and the next watcher tick attributes the GTT's fill through PK-7's broker-proof path. The result is one closure, one journal row at the GTT fill price, and one app SELL (test I1).
  - **Both fill.** The finalizer closes the position once, through the explicit exit, then finds the GTT `triggered`. It raises POSSIBLE DOUBLE SELL, deletes nothing and sends nothing. No synthetic exit is created (test I2).
- **Rules left intact:** PK-7's broker-proof closure is unchanged, and the confirm-mid-watcher-cycle race test was restated (section 20).

## 16. Emergency Exit behaviour

Emergency Exit uses the same lifecycle and never waits for fills. Each position's result is one of:

| Result | Meaning |
|---|---|
| `exit_submitted` | the order was accepted; the position stays open (EXITING) |
| `exit_outcome_unknown` | a claimed or UNKNOWN exit already exists (PK-2), or this click's submission was ambiguous |
| `already_exiting` | new: an EXITING or partial exit already owns the position; nothing is sent |
| `refused` | refused at confirmation (PK-4) |
| `failed` | the broker rejected the order |
| `closed` | the fill is already proven (not reachable for live today) |
| `risk_check_unavailable` / `entry_cancelled` | unchanged |

The preview marks `exit_in_flight`. The UI toast reports the number of exit orders **placed** and says the positions stay open until the fills arrive. Any position that was not exited, including the `already_exiting` and `failed` results, is listed in a sticky error toast. The system event now reads "exit requested for N position(s) …".

## 17. Risk and open-position behaviour while EXITING

An EXITING or partial position is still counted by:

- `list_open_positions`;
- `_open_position_count` (max open positions);
- `_open_risk_sum` (portfolio risk);
- the Emergency Exit preview;
- reconciliation's position-state check;
- the GTT watcher's monitored set.

Test A asserts `([position], 1 open, ₹500 risk)` while EXITING; test C asserts `([], 0, 0)` after the fill.

The strategy exit scanner treats an EXITING exit as pending, so it does not create duplicate exit tickets.

## 18. Journal and closed_at rules

| Exit state | Journal | `closed_at` |
|---|---|---|
| broker COMPLETE, full quantity, average price | written once, at the broker `average_price`, dated by the fill | fill time, conditional `WHERE closed_at IS NULL`, rowcount checked |
| OPEN or AMO | no | NULL |
| PARTIAL (working or ended) | no | NULL |
| REJECTED or CANCELLED | no | NULL |
| UNKNOWN, or adopted-not-filled | no | NULL |
| COMPLETE without a usable fill | no (flagged) | NULL |
| dry run | written immediately | simulation time |

There are three safeguards, and they are layered:

1. The compare-and-set on the exit's fill claim.
2. `record_closed_trade`'s existing-row check. It also now raises on any exit whose `entry_fill_status` is not NULL or `filled`.
3. The unique `idx_closed_trades_sell_ticket`.

`backfill_closed_trades`, which runs at every startup, now only picks up exits whose fill is proven. Before PK-8, a restart would have journaled every EXITING exit (test L).

## 19. UI changes (minimal; the PK-19 redesign was not started)

- **Exit ticket badges:**
  - **EXITING · awaiting fill** (amber, solid border) for `exit_state='exiting'`;
  - **PARTIAL EXIT — NEEDS REVIEW** (the PK-2 dashed style) for `exit_partial`/`exit_unconfirmed`, and the row is flagged.
- **Position rows** show an "exiting" chip when `exit_in_flight_ticket_id` is set. The badge never says closed.
- **The PK-2 UNKNOWN / NEEDS REVIEW badges** are unchanged and take precedence, because they are `confirmed` rows.
- **Approval toast:** "Exit placed — the position stays open, with its protective GTT, until Kite reports the fill", with an AMO variant.
- **Emergency Exit toast:** section 16.

## 20. Tests converted from xfail

- `test_p0_7_unfilled_exit_keeps_protection_and_position_open[limit_market_hours]`
- `test_p0_7_unfilled_exit_keeps_protection_and_position_open[market_amo_after_hours]`

Only the marker was removed; the body is unchanged.

Existing tests restated because they encoded "closed on acceptance". Each has a comment saying why.

| Test | Now asserts |
|---|---|
| `test_baseline_api::test_exit_ticket_closes_position_and_records_trade` | EXITING after confirm, closed after one monitor cycle |
| `test_baseline_api::test_emergency_exit` | `exit_submitted`, then closed after the monitor |
| `test_review_changes::test_order_monitor_rejected_exit_reopens_position` → `…_keeps_position_open_and_protected` | never closed, GTT still active, `kite_gtt_id` kept, alert says "still active" |
| `test_review_changes::test_order_monitor_exit_fill_reprices_journal` → `…_journals_at_fill_price` | no journal before the fill; journal at 3150 after |
| `test_review_changes::test_executed_exit_does_not_leave_phantom_open_risk` | risk 500 while EXITING, 0 after the fill |
| `test_live_safety::test_p0_3_emergency_exit_leaves_dry_run_positions_alone` | monitor cycle added before the closure assertion |
| `test_live_safety::test_p0_4_duplicate_exit_is_superseded_and_cannot_sell_again` | sibling refused (`EXIT_ALREADY_IN_PROGRESS`, still pending) while EXITING, superseded after the fill |
| `test_live_safety::test_p0_4_emergency_exit_continues_past_a_refused_position` | INFY is `exit_submitted` |
| `test_gtt_watcher::test_explicit_exit_confirmed_mid_cycle_is_not_duplicated` | the GTT survives the racing confirm; after the fill there is one exit, one journal row, the GTT deleted, and no watcher flags |

## 21. New tests (`tests/test_exit_lifecycle.py`, 21 cases)

| Case | Test |
|---|---|
| A | open LIMIT exit stays EXITING through 3 monitor+watcher cycles; UI fields; still counted for risk |
| B | AMO across an evening and night (pinned monitor clock); closes on the next-day fill at the fill time and `exit_date` |
| C, M | COMPLETE closes once at the broker fill price and time; 4 repeated cycles give one journal row, one `closed` event and one `delete_gtt` |
| — | COMPLETE with average price 0 is never journaled at the reference price; flagged once; closes when the price appears |
| D, E | REJECTED and CANCELLED leave the position exactly as it was (parametrized) |
| F | partial while working: flagged once per filled quantity, not closed |
| F | partial then ended: `exit_partial`, needs review, GTT kept, sibling refused, Emergency Exit `already_exiting`, no new order |
| G | UNKNOWN exit adopted OPEN stays EXITING and protected |
| H | UNKNOWN exit adopted, then COMPLETE, closes on the fill |
| H | UNKNOWN exit whose order already filled closes in the adopting cycle |
| I | GTT fills while the exit is OPEN, then the exit is rejected: exactly one closure (PK-7 path), one app SELL |
| I | explicit exit and GTT both fill: one closure, POSSIBLE DOUBLE SELL, no `delete_gtt`, no synthetic exit |
| J | sibling refused while EXITING; superseded after the fill, or approvable after a rejection (parametrized) |
| — | the exit scanner does not recreate an exit for an EXITING position |
| K | Emergency Exit reports `exit_submitted`; position open; preview `exit_in_flight`; second Emergency Exit gives `already_exiting`; closes on the fill |
| L | persisted EXITING row survives two `init_db()` restarts (no backfill journal); resolved by a later cycle; a third restart adds no row |
| — | `record_closed_trade` refuses an unfilled exit |
| N | `delete_gtt` fails after the fill: still closed, journaled once, ORPHANED GTT urgent alert and mismatch, `kite_gtt_id` kept |
| — | alert-action sell of an untracked holding completes without closing or journaling anything |

There are no sleeps. Broker outcomes are driven through FakeKite, and FakeKite was not extended.

## 22. Mutation-test results

The mutation set was run once on the final code by a scratch script. For each mutant it saved the original files, applied one mutation, ran the targeted files (`test_exit_lifecycle`, `test_live_safety`, `test_review_changes`, `test_baseline_api`, `test_gtt_watcher`: 225 tests), restored the files and compared hashes.

| # | Mutation | Result |
|---|---|---|
| 1 | delete the GTT immediately after `place_order` | **caught**, 16 failures |
| 2 | stamp `closed_at` on exit submission | **caught**, 19 failures |
| 3 | journal on exit submission (with the journal fill guard also disabled) | **caught**, 15 failures |
| 4 | treat an OPEN exit as closed | **caught**, 13 failures |
| 5 | treat an AMO exit as closed | **caught**, 4 failures (including the converted P0-7 AMO test) |
| 6 | treat a partial fill as a full close | **caught**, 1 failure (`test_f_partial_fill_then_ended…`) |
| 7 | skip order-monitor finalization | **caught**, 20 failures |
| 8 | duplicate journal row on repeated monitor cycles: re-select filled exits, unconditional claim, idempotency check removed | **caught**, 5 failures |

Mutation 8 was rerun alone to see how it fails: the storage-level `idx_closed_trades_sell_ticket` raises `IntegrityError: UNIQUE constraint failed`, so even with both application-level guards removed, the duplicate row cannot be written.

After the run, the SHA-256 of every mutated file matched its original:

| File | SHA-256 |
|---|---|
| `backend/execution.py` | `4805045c…` |
| `backend/journal.py` | `82a563c7…` |
| `backend/order_monitor.py` | `e6a25424…` |
| `backend/routes/tickets.py` | `194de936…` |

No code changed after the mutation run.

## 23. Full-suite results

These are three runs on the final code, after the mutation run.

| Run | passed | xfailed | failed | xpassed |
|---|---|---|---|---|
| 1 | 455 | 0 | 0 | 0 |
| 2 | 455 | 0 | 0 | 0 |
| 3 | 455 | 0 | 0 | 0 |

455 = the 432 previously passing + the 2 converted P0-7 tests + 21 new.

## 24. Repeated targeted and race-test results

Five consecutive runs of `test_exit_lifecycle`, `test_live_safety`, `test_gtt_watcher`, `test_gtt_closure_proof` and `test_review_changes` each gave **249 passed**, 0 failed. These include the thread and barrier races from PK-1, PK-2 and PK-4, and the PK-7 watcher races. No flakes were seen.

## 25. Database migration validation

PK-8 changes no schema, index or migration code, and `backend/db.py` is identical to the pre-PK-8 snapshot. The only startup-path change is the `backfill_closed_trades` predicate. As a check, `init_db()` was run twice on a scratch copy of `kite_terminal.db.pre_pk8_bak`:

- `integrity_check` = ok before, after run 1 and after run 2;
- row counts are unchanged for all 28 tables: `trade_tickets` 18, `closed_trades` 0, `ohlc_daily` 50,556, `instrument_cache` 10,133 and so on;
- run 2 is idempotent: identical schema and row counts.

Run 1 did change the scratch schema, but not because of PK-8. It applied the **pending PK-2 migration**, which adds the `order_tag`, `submission_state` and `submitted_at` columns and the `submission_unknown` event type through the existing `trade_tickets`/`ticket_events` rebuilds, and the **PK-4 exit-owner index**. The live DB has not been started under PK-2+ code yet. These migrations will run on the app's next start; the backup above covers that.

The live DB was untouched by all of this work. `backend/kite_terminal.db` SHA-256 (`1378f8b4…`) and mtime (2026-09-22 23:03:50) are identical before and after. The tests use a per-test temporary DB via `conftest`.

The live DB holds **no** exit tickets (checked read-only), so there is no in-flight pre-PK-8 exit whose position was already closed early.

## 26. Remaining limitations, intentionally deferred

- **PK-13 (partial fills).** A partial exit keeps the position blocked, with `exit_partial` and an urgent alert, until a human resolves it in Kite. The app does not track the filled quantity per position, does not shrink the GTT, and offers no in-app resolve action. The journal records nothing for a partial.
- **P1-1 (manual order cancel).** Cancelling an EXITING exit in Kite is handled, because it shows up as CANCELLED and the position stays open. No in-app "cancel exit" button was added.
- **Orphan-GTT reverse reconciliation (P1).** After an ORPHANED GTT alert, nothing re-attempts the delete. The alert, event and `reconciliation_log` row are the record.
- **Trailing and breakeven while EXITING.** The watcher may still ratchet the GTT, and breakeven proposals may still be offered. Both are harmless, but not frozen.
- **The GTT–fill race window.** The GTT could fire in the moment between an exit's COMPLETE and its deletion. This is flagged as POSSIBLE DOUBLE SELL, not prevented. In practice Kite's CNC holdings block also usually rejects the second SELL.
- **Emergency Exit per-position enhancements** beyond the statuses above (P1), sector mapping, the exposure basis, the trading lock, secrets and cloud hardening, the paper broker, backtest realism, and Smart Money/volume/option intelligence are untouched.

## 27. Deviations from the audit recommendation

1. **No new `exiting` status value.** EXITING is represented by the existing `executed` + `entry_fill_status='pending'`, named for the API and UI by `exit_state()`. A new status would have needed a `trade_tickets` table rebuild (because of the status CHECK constraint), a change to the PK-4 unique index predicate, and changes to every `status = 'executed'` query. Keeping `executed` leaves the PK-4 storage invariant covering in-flight exits unchanged. The trade-off is that "executed" on an exit ticket no longer means "exited". Every consumer that needed the distinction now checks the fill: risk, journal, backfill, Emergency Exit, the scanner and the UI.
2. **GTT kept until the fill, then deleted.** This is the brief's preferred rule. It was not replaced by modifying the GTT or reducing its quantity.
3. **The partial-fill alert is de-duplicated per distinct filled quantity** through `reconciliation_log`, not raised on every cycle.
4. **The reopen logic was removed outright,** with no legacy branch. The live DB holds no exit tickets, so no pre-PK-8 in-flight exit exists to reopen. A pre-PK-8 journal row, if one ever existed, is still handled: it is re-priced at the fill instead of being duplicated.
