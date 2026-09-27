# OPUS55 PK-7 Implementation Report: a position closes only on broker proof (P0-6)

| | |
|---|---|
| Packet | PK-7 only. It fixes **P0-6** for trailing and OCO positions alike. PK-8 (P0-7) and PK-13 (partial-fill model) were not started. |
| Baseline before | 370 passed, 4 xfailed (P0-6 ×2, P0-7 ×2), 0 failed, 0 xpassed. Re-measured before any edit. |
| Result after | **432 passed, 2 xfailed, 0 failed, 0 xpassed** (3 identical full runs on the final code). Only the two P0-7 xfails remain. |
| Backups taken first | Tag `backup-pre-pk7` and branch `backup/pre-pk7` (both at `392f414`). Tag `backup-pre-pk7-worktree` (commit `c70ca5c`) is a snapshot of the whole working tree, including the uncommitted PK-0 to PK-5 files. It was built through a temporary index, so the real index, HEAD and working tree were not touched. `backend/kite_terminal.db.pre_pk7_bak` was taken with the SQLite online-backup API; `integrity_check` = ok. |
| Schema | Unchanged. No migration, no new column, no new `ticket_events` type. |
| Committed | No. Nothing was committed, as instructed. |

## 1. Executive summary

Before PK-7, when a protective GTT was `triggered` or missing from `get_gtts()`, the GTT watcher closed the position even when no fill existed. It did the following:

- priced the exit from `kite.ltp()`, or from the stop if that failed;
- labelled the exit `estimated`;
- stamped `closed_at`;
- journaled the trade.

A triggered GTT only means that Kite *placed* a SELL LIMIT. After a gap-down through that limit, the order sits unfilled or part-filled. The shares are still held, but the app showed the position flat and gave no alert. PK-6 extended the same behaviour to OCO positions.

PK-7 replaces "the GTT is no longer active" with **broker evidence**:

- **Closes the position.** A COMPLETE SELL for the position's full quantity. It is identified by Kite's own GTT-to-order link (`orders[i].result.order_result.order_id`), or, when there is no link, by a single, unambiguous order match. The exit is recorded at the broker's `average_price` and fill time.
- **Keeps the position OPEN.** An unfilled, partly filled, failed, ambiguous or unidentifiable exit, and a GTT that is missing, deleted or expired without a fill. The evidence is written to `reconciliation_log`, an urgent alert is broadcast once per distinct state, and no order is sent.
- **Never used.** The LTP and a fabricated price.
- **Changes nothing on its own.** A drop in the broker's held quantity is supporting evidence in the alert text only; it never closes a position.

Both P0-6 xfails are now ordinary passing tests. There are 60 new tests. Thirteen unsafe mutants were each killed, and the whole pre-PK-7 engine (M0) fails 52 of the 62 targeted tests.

## 2. Files changed

This is PK-7's own diff, measured against `backup-pre-pk7-worktree`.

| File | Change |
|---|---|
| `backend/trailing_engine.py` | +342/−88. It adds the evidence model (`ExitEvidence`, `_exit_evidence`, `_linked_evidence`, `_matched_evidence`, `_classify` and helpers) and the decision in `_handle_inactive_gtt`. There are three new outcome handlers: `_handle_exit_not_filled`, `_handle_exit_failed` and `_handle_closure_needs_review`. `_find_matching_fill` is removed. `_handle_gtt_closure` now requires a fill and uses the broker fill time; its LTP and stop fallback and the `estimated` status are gone. `_handle_protection_lost` now carries a held-quantity note and de-duplicates its alert. `_process_ticket` receives the GTT records, not just their statuses. |
| `backend/reconciliation.py` | +16/−8. `log_mismatch` returns `True` when it writes a row. `_check_gtt_state` records the actual state (`GTT triggered` or `GTT missing`) and no longer promises that the position "will be closed". |
| `tests/fake_kite.py` | +45/−16, generic only. `trigger_gtt` records Kite's `result`/`order_result` link on the fired leg. It gains `expose_result=False` to simulate a missing link, and `placement="failed"` with `rejection_reason`, where no order is created. Order timestamps are now IST, as Kite's are, and `exchange_timestamp` is set once an order fills. `add_broker_order` accepts `order_timestamp`. |
| `tests/test_gtt_closure_proof.py` | **New**: 20 test functions and 60 test cases (§19). |
| `tests/test_live_safety.py` | Removed the P0-6 `@_xfail`. `test_p0_4_gtt_firing_while_an_exit_is_unresolved…` now makes INFY's stop actually fill with `trigger_gtt(COMPLETE)` instead of only setting `triggered`. Its assertions are unchanged. |
| `tests/test_review_changes.py` | `test_trailing_triggered_gtt_is_detected_as_closed` became `…_without_exit_evidence_stays_open`. The audit named this test as needing an update. |
| `tests/test_baseline_api.py` | `test_trailing_detects_vanished_gtt_and_records_close` became `test_trailing_vanished_gtt_without_fill_is_flagged_not_closed`. The audit named this one too. |
| `tests/test_gtt_watcher.py` | Docstring and comment only; no assertion changed. |
| `docs/OPUS55_PK7_IMPLEMENTATION_REPORT.md` | This report. |

The following were **not** touched:

- `execution.py`, `order_monitor.py`, `journal.py`, `risk_engine.py`, `db.py`, all routes and `kill_switch.py`;
- the frontend;
- `docs/OPUS55_AUDIT_REPORT.md`.

`order_monitor.TERMINAL_STATUSES`, `order_monitor._order_from_history`, `risk_engine.get_held_quantity` and `risk_engine.list_open_positions` are reused as they are.

## 3. Previous unsafe closure flow

This was traced in the code before editing.

1. **A.** `_tick_sync` made one `get_gtts()` call and built a map of GTT id to status. Any status other than `active` entered the closure branch; `None` meant the GTT was missing from the book.
2. **B.** `_find_matching_fill` called `orders()` and matched symbol, exchange, side, `COMPLETE` and `filled_quantity == quantity`, then took the **most recent** match. If `orders()` failed, it returned `None`.
3. **C.** `if status in (None, 'triggered') or fill is not None` led to `_handle_gtt_closure`. That function ran the PK-4 owner check. Then, **if there was no fill**, it priced the exit at `kite.ltp()` (or the stop), inserted an executed synthetic SELL with `entry_fill_status='estimated'`, stamped `closed_at = now`, superseded sibling exits and journaled.
4. `deleted`, `cancelled`, `expired`, `disabled` or `rejected` with no fill led to `_handle_protection_lost`.
5. **D.** `risk_engine.get_held_quantity` (holdings `quantity + t1_quantity` plus net positions) existed but was not used here.
6. **E.** PK-4 ownership was checked only inside the closure path.
7. **F.** PK-6 routed trailing and OCO positions through this same branch.

The consequence: a failed `orders()` call, a gap-down unfilled LIMIT, a partial fill, and a GTT deleted by hand and missing from the book all became a "closed at LTP" trade.

## 4. New broker-proof closure rules

For a watched position whose GTT is no longer `active`:

1. **Ownership first (PK-4).** If an exit ticket owns the position (`confirmed` or `executed`), the watcher flags it ("POSSIBLE DOUBLE SELL") and does nothing else. A GTT event never mutates a position that an explicit or UNKNOWN exit owns.
2. **Read the order book.** A failure is **not** caught as "no fill". The exception propagates, the PK-6 per-position isolation rolls the position back, retries it next cycle and escalates after 3 failures in a row.
3. **Gather evidence.** See §5.
4. **Decide.** Only `filled` closes the position. Every other outcome keeps it OPEN (§7 to §12).

Acceptable proof of closure is **one identified broker SELL order, COMPLETE, whose filled quantity is at least the position's quantity**. Nothing else is proof: not the GTT status, not the GTT disappearing, not the LTP, not proximity to the stop or target, and not a drop in held quantity.

## 5. Matching-order algorithm

**Direct link, when the GTT record carries a fired-leg `result`.** Kite's documented GTT payload gives each fired leg `result.order_result.{status, order_id, rejection_reason}`.

- More than one fired leg gives `ambiguous`.
- No `order_id` gives `exit_failed`, carrying Kite's `rejection_reason`. Kite could not place the order, for example because of a circuit limit.
- Otherwise the order is looked up in `orders()`, then in `order_history(order_id)`. If it is not found, the result is `none`: "not in Kite's order book".
- The linked order must still match symbol, exchange, side and quantity, or the result is `ambiguous`.
- A matching order goes through `_classify`.

**Fallback match, when there is no link** (the GTT is missing from `get_gtts()`, or lists no result). An order is a candidate only if **all** of these hold:

| Filter | Why |
|---|---|
| symbol, exchange, `transaction_type = exit side`, order `quantity = position quantity` | The GTT leg quantity is the position quantity (`execution._execute_live`). |
| `product` equals the ticket's `product_type`, when the order has a product | Never mistake an MIS sell for a CNC exit. |
| not placed before the position's `executed_at` (UTC converted to IST, compared with `order_timestamp`) | A SELL placed before the entry cannot be its exit. An unreadable time keeps the candidate, which must still be the only plausible one. |
| not an order the app already accounts for: any `trade_tickets.kite_order_id`, any earlier `gtt_auto_trigger.matched_order_id`, or any order carrying the app's own tag (`^kt\d+x…`) | Another ticket's order is never this position's exit, and GTT-placed orders carry no tag. |
| only when the GTT is `triggered`: the order price equals one of the GTT's leg prices | A GTT's order carries its leg price exactly. This is not applied to a deleted or missing GTT, so a whole-position SELL placed by hand in Kite still counts as a real exit. |

The candidates are then resolved:

- **Live candidates** are those with a fill, or an order still working. Two or more give **`ambiguous`**: nothing is guessed, and there is no "most recent wins".
- If there are no live candidates but some matched orders ended unfilled, the result is `exit_failed`. If there are none at all, the result is `none`.
- **Same-symbol guard.** If any other open app position exists in the same symbol and exchange, an unlinked match gives `ambiguous`. Only Kite's link can attribute a fill when app positions share a symbol (§12).

**`_classify(order)`:**

- `COMPLETE` with `filled ≥ quantity` gives `filled`;
- `0 < filled < quantity` gives `partial`, whether the order is still working or has ended;
- 0 filled and terminal gives `exit_failed`;
- 0 filled and still working gives `unfilled`.

## 6. Quantity-reduction evidence rules

`risk_engine.get_held_quantity` (holdings `quantity + t1_quantity` plus net positions) is read only for the `none`, `exit_failed` and `ambiguous` outcomes and for lost protection. Its result is quoted in the alert:

- "Kite now shows no TCS held — the shares appear to have gone, but the order and price that sold them cannot be identified.";
- or "Kite shows 5 TCS held (this position is 5).";
- or "could not be read".

**Chosen rule: a quantity drop alone never closes a position.** This is the safer of the two options the brief allowed, for three reasons:

- the figure covers the whole symbol, including shares held outside the app and other app positions;
- it cannot say which order sold the shares or at what price;
- the journal model (`closed_trades.exit_price`, and the synthetic exit's `entry_price`) cannot represent a close without a price, and a price must not be invented.

"Held 0 with no identifiable order" is therefore flagged **CLOSURE NEEDS REVIEW** and stays OPEN. This is pinned by test I and mutant M13.

## 7. Full fill behaviour

This is the existing good path, preserved. `_handle_gtt_closure(conn, ticket, fill, linked)` does the following:

- inserts one executed synthetic SELL (`closes_ticket_id` set);
- sets `entry_fill_status='filled'`, and `filled_price` and `entry_price` to the broker's **`average_price`**;
- sets `executed_at`, `filled_at` and the original's `closed_at` to the broker **fill time** (`exchange_timestamp`, else `order_timestamp`, IST converted to UTC), falling back to now only if Kite gives no time;
- records `matched_order_id`, `closer_to` and a new `evidence` field in `rule_checklist`: `gtt_order_link` or `order_match`;
- supersedes sibling exits;
- writes exactly one `closed_trades` row;
- broadcasts `tickets_changed/gtt_closed`.

The PK-4 `IntegrityError` backstop is kept. There is no LTP call on any path; tests assert `calls_to("ltp") == []`.

## 8. Partial fill behaviour

This is an interim rule until PK-13.

| Order state | Position | `kite_gtt_id` | Recorded |
|---|---|---|---|
| partly filled, still working | OPEN, `closed_at` NULL | **kept**: re-checked every cycle, and closes if the order completes | no exit, no journal |
| partly filled, then cancelled or rejected | OPEN | NULL: the GTT is spent and the order is over | no exit, no journal |

Each distinct state writes one reconciliation row (`actual` = `exit order <id> <status> <filled>/<qty> filled`), one `modified` ticket event and one urgent `trailing_error`. The alert reads **"GTT EXIT PARTIALLY FILLED — POSITION NOT CLOSED"** and includes the filled quantity, the average price and the shares still held. It adds "UNPROTECTED" once the order has ended. Progress from 2/5 to 3/5 re-alerts; an unchanged state does not. The observed partial quantity, order id and average price stay in `reconciliation_log`, where PK-13 can use them.

## 9. Unfilled behaviour

Take the gap-down case: the GTT is triggered and its SELL LIMIT is OPEN with 0 filled.

- The position stays OPEN, `closed_at` stays NULL, and there is no synthetic exit and no journal row.
- `kite_gtt_id` is kept, so the watcher re-checks each cycle. When the LIMIT later fills, the position closes at that fill, once. Test A covers this end to end.
- One urgent alert fires: **"GTT TRIGGERED — EXIT NOT FILLED — POSITION MAY BE UNPROTECTED"**, with the order id, `OPEN 0/5 filled`, the limit price, and advice to modify or cancel the order in Kite.
- No SELL is ever sent automatically.

## 10. Missing, deleted, expired or cancelled GTT

A status of `deleted`, `expired`, `cancelled`, `disabled` or `rejected`, or a GTT absent from `get_gtts()`, with **no fill evidence**, goes to `_handle_protection_lost`:

- the position stays OPEN;
- `kite_gtt_id` is set to NULL, because nothing is left to watch;
- one reconciliation row is written: `GTT deleted`, `GTT expired`, …, or `GTT missing`;
- one urgent alert fires: **"PROTECTION MISSING — POSITION STILL OPEN: … still OPEN but UNPROTECTED … Place a stop-loss in Kite."**, with the held-quantity note.

If a genuine whole-position SELL exists (for example the user deleted the GTT and sold in Kite), it is real broker evidence and closes the position at that fill (`evidence = order_match`).

A GTT that is `triggered` but has no identifiable order (Kite lag, or the order book has rolled over to a new session) is **not** treated as lost protection. Its `kite_gtt_id` is kept, it is flagged **CLOSURE NEEDS REVIEW**, and a fill that appears later still closes it.

When the GTT fired but its order could not be placed (a failed `order_result`), or its order ended with 0 filled, the position stays OPEN, `kite_gtt_id` is set to NULL, and the alert reads **"GTT TRIGGERED BUT ITS EXIT ORDER FAILED … still OPEN but UNPROTECTED"**, with Kite's reason.

## 11. Several plausible orders

Two or more plausible unlinked SELLs give **CLOSURE NEEDS REVIEW** listing every candidate (id, status, filled/quantity). The position stays OPEN, `kite_gtt_id` is kept, and there is no journal row. This applies whether the GTT is missing or triggered without a link. The old "sort by `order_timestamp`, take the first" rule is gone (mutant M5).

## 12. Same symbol and several positions

The app can hold several open positions in one symbol; tests from PK-6 already rely on this. Neither the aggregate held quantity nor an unlinked SELL can say which position was sold, so:

- an unlinked match is refused whenever another open app position (`risk_engine.list_open_positions`) exists in the same symbol and exchange. The flag names those position ids;
- Kite's GTT link still attributes the fill precisely. Test H closes exactly the linked position and leaves the other untouched;
- a quantity drop never closes anything (§6).

## 13. Interaction with PK-4 exit ownership

- The owner check now runs **first**, for every non-active outcome, not only inside the closure path. With a `confirmed` or `executed` exit owning the position, a fired GTT, filled or unfilled, only raises the existing "POSSIBLE DOUBLE SELL" flag. There is no sibling exit, no closure and no journal row (tests J ×4 and K, mutant M9).
- The `idx_trade_tickets_one_exit_owner` backstop is unchanged. A race still ends in the flag, not a second exit.
- The PK-6 tests H (explicit exit mid-cycle) and I (UNKNOWN exit, GTT fills), and PK-4's `test_p0_4_gtt_firing_while_an_exit_is_unresolved…`, pass unchanged in their assertions.

## 14. Interaction with the PK-6 watcher

- `monitored_positions()`, per-position isolation, `TokenException` handling, the LTP batch and the threading are unchanged.
- `_process_ticket` now receives the GTT **records**, because the link lives in them. The trailing LTP batch still uses the status map.
- The same rules apply to trailing (single-leg) and OCO positions. Test L runs 8 scenarios × both kinds against one expected-outcome table; the PK-6 relative parity test also still passes.
- **Broker-call budget.** Nothing changes while every GTT is active: `orders()` is still called only for non-active GTTs. `holdings()` and `positions()` are added only for the rare no-proof outcomes, so the held quantity can be quoted. `order_history()` is used only for a linked order missing from `orders()`.

## 15. Reconciliation changes

- `log_mismatch` returns whether it wrote a row. The watcher's `_flag` helper uses this to alert and log a ticket event only when a new or changed finding is recorded.
- `_check_gtt_state` logs `GTT triggered in kite.get_gtts()` or `GTT missing in kite.get_gtts()` rather than one generic string, and its detail says that closure needs a proven broker fill.
- The watcher's rows distinguish the states. `order_status` rows: `exit order … OPEN 0/5 filled` (unfilled), `… 2/5 filled` (partial), `GTT … fired; exit order failed`, `GTT deleted` / `GTT missing` (lost protection). `position_state` rows: `GTT triggered; closure unproven: …; held N` (review or ambiguous), and the existing owner flag.
- Orphan-GTT reverse reconciliation was not attempted; it remains PK-16.

## 16. Journal changes

| Case | `closed_trades` |
|---|---|
| Full broker-proven exit | one row, at the broker average price and fill date |
| Unfilled | none |
| Partial | none (no full-position row) |
| Ambiguous, or a quantity drop without an order | none |
| Missing or deleted GTT without proof | none |

`journal.py` itself is unchanged. `entry_fill_status='estimated'` is no longer produced anywhere.

## 17. UI and alert changes

There is no frontend change. The existing infrastructure already surfaces everything:

- `trailing_error` broadcasts become urgent banner chips (`app.js`);
- unresolved `reconciliation_log` rows are listed as persistent urgent chips until marked reviewed;
- when `kite_gtt_id` is NULL on an open live position, the existing "protective stop FAILED — UNPROTECTED" chip also appears.

New alert headlines:

- `GTT TRIGGERED — EXIT NOT FILLED — POSITION MAY BE UNPROTECTED`
- `GTT EXIT PARTIALLY FILLED — POSITION NOT CLOSED`
- `GTT TRIGGERED BUT ITS EXIT ORDER FAILED`
- `CLOSURE NEEDS REVIEW`
- `PROTECTION MISSING — POSITION STILL OPEN`

## 18. Tests converted from xfail

| Test | Before | After |
|---|---|---|
| `test_live_safety.py::test_p0_6_triggered_gtt_without_fill_keeps_position_open[unfilled]` | xfail (strict) | **pass** |
| `test_live_safety.py::test_p0_6_triggered_gtt_without_fill_keeps_position_open[partial]` | xfail (strict) | **pass** |

After the engine change, the first full run showed exactly these two as `XPASS(strict)`. Three other tests failed: the two the audit names as encoding the unsafe behaviour, and the INFY half of a PK-4 test that relied on "triggered = closed". Nothing else moved. The marker was then removed, without changing any assertion.

## 19. New tests (`tests/test_gtt_closure_proof.py`, 60 cases)

| Req. | Test | Asserts |
|---|---|---|
| A | `test_gap_down_unfilled_stop_keeps_position_open_until_the_exit_fills[oco, trailing]` | Open, GTT kept, no exit or journal, no SELL, no LTP; one alert with the exact wording; not repeated on the second cycle. When the order later fills, the position closes at that fill, once. |
| B | `test_partial_fill_is_not_a_close_and_each_change_is_flagged[oco, trailing]` | 2/5 then 3/5: open, flagged per change, no journal. Cancelled: GTT NULL, "UNPROTECTED", no further alerts. |
| C, D | `test_full_fill_closes_at_the_broker_fill_price_and_time[oco_stop, oco_target, trailing_stop]` | Closed at `average_price`; `executed_at`, `filled_at` and `closed_at` equal the broker fill time in UTC; `exit_date` is correct; one journal row; `closer_to` is correct; `evidence=gtt_order_link`; no LTP. |
| E, F | `test_gtt_gone_without_a_fill_is_protection_missing_not_a_close[6 states × 2 kinds]` | deleted, expired, cancelled, disabled, rejected and missing: open, GTT NULL, "PROTECTION MISSING", held note, one flag. |
| E | `test_gtt_deleted_and_the_position_sold_by_hand_in_kite_closes_at_that_fill` | A real manual exit closes the position at its fill (`order_match`). |
| G | `test_several_plausible_sell_orders_are_never_guessed_between[missing, triggered_without_link × 2 kinds]` | Open, review, every candidate listed. |
| H | `test_an_unlinked_sell_is_not_attributed_when_app_positions_share_the_symbol` | Neither position is closed; the flag names the other position. |
| H | `test_the_gtt_link_closes_exactly_the_right_one_of_two_same_symbol_positions` | The linked one closes; the other is untouched. |
| I | `test_broker_holding_nothing_without_an_identifiable_order_is_review_not_a_close[2 kinds]` | Held 0 with no order: open, review, quotes "no TCS held", no exit. |
| I | `test_linked_exit_order_missing_from_the_book_is_review_not_a_close` | Link present but the order is gone: review, not a close. |
| — | `test_linked_exit_order_found_through_order_history_closes` | The `order_history` fallback. |
| — | `test_gtt_whose_exit_order_kite_could_not_place_leaves_the_position_open_and_unprotected[2 kinds]` | A failed `order_result`: open, GTT NULL, Kite's reason in the alert. |
| — | `test_a_sell_placed_before_the_position_was_entered_is_not_its_exit` | Timing filter. |
| — | `test_an_order_the_app_already_accounts_for_is_not_this_positions_exit[another_ticket, app_tag, earlier_gtt_closure]` | Known-order exclusion. |
| — | `test_without_a_link_a_triggered_gtts_order_must_carry_one_of_its_leg_prices` | Leg-price filter. |
| — | `test_an_order_the_gtt_links_to_is_still_checked_against_the_position` | A linked order with the wrong quantity gives review. |
| — | `test_order_book_outage_is_never_read_as_no_fill` | `orders()` fails 3 times: no change, no flag, escalated on the 3rd. It closes once the broker answers. |
| J | `test_gtt_firing_while_an_explicit_exit_owns_the_position_records_nothing[unfilled, filled × confirmed, executed]` | Only the owner flag; no second exit, journal or SELL. |
| K | `test_gtt_fired_unfilled_while_an_unknown_exit_exists_creates_no_sibling` | No sibling; the UNKNOWN exit is left `unknown`; nothing re-sent. |
| L | `test_trailing_and_oco_positions_follow_the_same_proof_rules[8 scenarios × 2 kinds]` | One expected-outcome table for both kinds; never a SELL or an LTP call. |

No test sleeps. Nothing reaches the network; `get_kite` is FakeKite throughout.

## 20. Mutation-test results

`mutate_pk7.py`, a scratchpad script, applied each mutant to `backend/trailing_engine.py`. It then ran the targeted set: the new file, the P0-4, P0-5 and P0-6 GTT tests, `test_gtt_watcher.py` and the 2 updated tests, 96 tests in all. It restored the original bytes in a `finally` block and compared SHA-256 hashes. The script ran twice, the second time on the final code: **restored = True** both times, **0 survivors**.

| Mutant | Killed by (failures) |
|---|---|
| **M0** The whole pre-PK-7 engine (from the snapshot) | 52 of 62 targeted tests. The 10 that pass there pin behaviour that was already right: PK-4 ownership ×5, the linked full fill, and the `filled` and `deleted` parity rows. |
| **M1** triggered ⇒ unconditional close at LTP (brief) | 24, including both P0-6 tests, A, B, I and L |
| **M2** missing ⇒ unconditional close at LTP (brief) | 14, including E/F `missing`, G, H and the updated vanished test |
| **M3** partial fill treated as full (brief) | 5, including P0-6 `partial` and B |
| **M4** no evidence ⇒ close at LTP (brief) | 28 |
| **M5** ambiguous ⇒ pick one arbitrarily (brief) | 6 (G ×4, L ambiguous ×2) |
| **M6** same-symbol guard removed | H (unlinked) |
| **M7** entry-time filter removed | the SELL-before-entry test |
| **M8** `orders()` failure swallowed as "no order" | the order-book outage test |
| **M9** owner check not done first | 8 (J ×4, K, PK-4 P0-4, PK-6 H and I) |
| **M10** leg-price filter removed | the leg-price test |
| **M11** linked order not checked against the position | the wrong-quantity link test |
| **M12** app-known orders not excluded | the known-order tests ×3 |
| **M13** held 0 ⇒ close at LTP | 5 (I ×2, the missing-link test, the 2 updated tests) |

A separate script, `mutate_m0.py`, ran M0 and was also restored and verified (restored = True).

## 21. Full-suite results

| Run | Result |
|---|---|
| Baseline, before any edit | 370 passed, 4 xfailed |
| After the engine change, before converting | 2 P0-6 `XPASS(strict)` plus 3 tests encoding the unsafe behaviour (§18); nothing else moved |
| Full suite, run 1 (final code) | **432 passed, 2 xfailed** |
| Full suite, run 2 (final code) | **432 passed, 2 xfailed** |
| Full suite, run 3 (final code) | **432 passed, 2 xfailed** |

Every run had **0 failed and 0 xpassed**. 432 = 370 baseline + 2 converted P0-6 tests + 60 new tests. The three updated tests were renamed in place, so the count is unchanged. The same three runs had also passed before a final cosmetic tidy (line wrapping only); they were repeated on the final code.

## 22. Repeated targeted results

These were run on the final code.

| Selection | Runs | Result |
|---|---|---|
| PK-7 targeted set: the new file, the P0-4 and P0-6 tests, `test_gtt_watcher.py` and the 2 updated tests | 10 | 94 passed, every run |
| Ambiguous, partial, multi-position and known-order subset (`-k "ambiguous or several or partial or same_symbol or right_one or holding_nothing or accounts_for"`) | 20 | 16 passed, every run |

## 23. Remaining xfails

| Finding | Tests | Packet |
|---|---|---|
| P0-7 | `test_p0_7_unfilled_exit_keeps_protection_and_position_open[limit_market_hours, market_amo_after_hours]` | PK-8 |

There are 2 xfails, both strict, and neither XPASSes. PK-7 does not touch explicit exits.

## 24. Limitations intentionally deferred to PK-8, PK-13 and later

- **Explicit exits (P0-7, PK-8).** Exit placement still deletes the GTT, stamps `closed_at` and journals before any fill. This is unchanged, as required.
- **Partial fills (PK-13).** The app still shows the full original quantity for a part-filled position. Risk, exposure and Emergency Exit use that quantity. Emergency Exit would request the full quantity; for CNC, Kite normally rejects a SELL beyond the free holding while the GTT's SELL is still working. The alert tells the user to act in Kite. The observed filled quantity, order id and average price are kept in `reconciliation_log`.
- **No manual resolution route.** A position the broker no longer holds but with no identifiable order (for example, the app was offline on the day the GTT fired and the order book has since rolled over) stays OPEN and flagged. No endpoint yet lets a user record that close with a price. Until one exists, it needs manual handling. This is the price of refusing to invent a price.
- **Reviewing a persisting finding.** Marking a reconciliation row reviewed while the condition persists re-raises it on the next cycle. This is the existing reconciliation behaviour, and deliberate while the position is really at risk.
- **An OPEN GTT order and the app's own exits.** While a triggered GTT's SELL is still working, the app does not block a user or strategy exit of the same position; exit-lifecycle gating is PK-8. The alert names the working order so the user can cancel or modify it first.
- **Orphan GTTs and reverse reconciliation** remain PK-16.

## 25. Deviations from the audit recommendation

| Audit said | PK-7 did | Why |
|---|---|---|
| Close on "a matching COMPLETE exit order (allowing partials)" **or** held quantity "dropped by at least the ticket quantity (reuse `get_held_quantity`)" | Close **only** on an identified COMPLETE full-quantity SELL. The held quantity is supporting detail only. | The brief allows either option and asks for the safer one. The held quantity covers the whole symbol and cannot price or attribute the exit, and the journal cannot record a close without a price (§6). Partials never close (§8). |
| Set a visible state such as `exit_state='gtt_triggered_unfilled'` | No new column. The state is the open position, the `kite_gtt_id` kept on a non-active GTT, and an unresolved, state-specific reconciliation row. | Brief §12 asks for the smallest representation. Risk, Emergency Exit and reconciliation already treat it as open because `closed_at` is NULL. Persistent urgent chips already come from `reconciliation_log`. This avoids a migration. |
| Matching "has no direct GTT-to-order link" (old docstring) | Uses Kite's documented `orders[i].result.order_result.order_id` when present; heuristic matching only as a fallback | Kite does expose the link for triggered GTTs (Kite Connect v3 GTT docs). The fallback is stricter than before and never guesses. |
| — | The owner check was moved before evidence gathering | PK-4 §11: a GTT event never mutates a position another exit owns, whatever the evidence. |
| — | An `orders()` failure is no longer swallowed | Before PK-7 it meant "no fill", which meant "estimated close". It is now a per-position failure that is retried and escalated (PK-6 isolation). |
| — | Broker fill time used for the synthetic exit's `executed_at`/`filled_at` and the position's `closed_at` | Brief §7. The exit date is correct even when the fill is detected later. |
