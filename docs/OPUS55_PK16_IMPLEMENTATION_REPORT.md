# OPUS55 PK-16 Implementation Report: reverse (Kite → app) GTT reconciliation (P1-10)

| | |
|---|---|
| Packet | **PK-16** only (audit **P1-10**). PK-17 and later were not started. |
| Baseline before | **718 passed**, 0 failed, 0 xfailed, 0 xpassed, re-measured on the untouched code before the first edit. |
| Result after | **761 passed, 0 failed, 0 xfailed, 0 xpassed**, three consecutive runs. 761 = 718 + 43 new tests in `tests/test_gtt_reconciliation.py`. |
| Backups taken first | Tag `backup-pre-pk16` and branch `backup/pre-pk16`, both at `eb089e7` (the tree was clean). `backend/kite_terminal.db.pre_pk16_bak` was taken with the SQLite online-backup API (read-only, `immutable=1`; the WAL was empty): `integrity_check` ok, 18 tickets, 50,556 OHLC rows, 10,133 instruments. Backup SHA-256 `01a9904837d2…`. |
| Live DB | Untouched: SHA-256 `1378f8b47cd7…` and mtime identical before the backup, after the backup and after the migration check. Tests use a per-test temporary DB and FakeKite. |
| User decision | An unlinked active SELL GTT is an **urgent** `ORPHAN_GTT`, even if it is the user's own GTT for a long-term holding. **Mark Reviewed silences that exact GTT state** until its quantity, trigger or status changes, or its classification changes (for example, the app opens a position in that symbol). |

# Executive Summary

**Before PK-16,** reconciliation only checked **app → Kite**: that each open ticket's `kite_gtt_id` was active and, since PK-13, that it covered the protection quantity. Nothing looked the other way. An active broker GTT that no open position owned was invisible. That covered:

- a GTT left behind by a failed delete;
- a GTT on a position closed by hand in Kite;
- a second GTT created by hand.

Such a GTT can fire months later and sell shares bought for another purpose (audit P1-10 failure scenario).

**Now every cycle reads one broker snapshot.** Every **active** GTT in this account's book is mapped to exactly one live app position, by the **exact `kite_gtt_id` link only**. Every GTT that cannot be mapped is classified, stored as a structured finding and raised as an urgent alert:

| Code | Meaning |
|---|---|
| `ORPHAN_GTT` | active SELL GTT, no app ticket references it, and no open app position exists in that symbol |
| `GTT_ON_CLOSED_POSITION` | referenced only by app tickets that are no longer open (closed, or a cancelled entry) |
| `MULTIPLE_ACTIVE_GTTS` | unreferenced, on a symbol whose open positions already have their own active GTT, so they could be sold twice |
| `UNLINKED_POSSIBLE_GTT` | unreferenced, and the only plausible stop of the only unprotected open position in that symbol. It is reported, **never linked** |
| `AMBIGUOUS_GTT_OWNERSHIP` | one GTT id recorded on several open positions, or several candidate GTTs / positions |
| `GTT_QUANTITY_TOO_HIGH` / `TOO_LOW` | the owner's GTT covers more or fewer shares than its PK-13 protection quantity. The difference is shown |
| `GTT_PRICE_MISMATCH` | the stop trigger at Kite differs from the app's stop by more than half a tick, on two consecutive cycles |
| `GTT_MISSING` | the owner's GTT is not active (the existing forward check, now structured) |
| `BROKER_POSITION_SHORTFALL` | the broker holds fewer shares than the app's open positions (existing check, now structured). It names any GTT still armed on that symbol |

**Detection only.** Reconciliation never cancels, resizes, places or relinks anything, and never writes `kite_gtt_id` or any position field. The UI has no cancel button. It points to Kite, or to the existing Orders & Trades → GTTs cancel.

**Validation:**

- 43 new tests;
- 12/12 focused mutations caught (7 required + 5 extra);
- the targeted suite ×10 and the ambiguity subset ×20, all green;
- full suite ×3: 761 passed each run;
- the migration run twice on a scratch copy of the new backup.

# Previous Reconciliation Direction

Traced before any edit:

| Question | Finding |
|---|---|
| A. How GTTs are fetched | `kite.get_gtts()` returns **every** status (active, triggered, cancelled, deleted, expired, disabled). The forward check fetched it only when an open ticket had a GTT, and on failure logged and returned (it did not flag). The trailing watcher reads its own book each tick. `sync_protection` and `remove_protective_gtt` read it ad hoc. |
| B. How positions reference GTTs | `trade_tickets.kite_gtt_id` is the only link. It is set at execution (`execution._execute_live`, or `routes/tickets._reconcile_late_result` for an adopted order). It is set to NULL after a successful delete (`position_quantity.remove_protective_gtt`, `order_monitor` for a cancelled entry, `routes/orders.cancel_gtt`) and when a GTT is spent (`trailing_engine`: exit not filled or failed, protection lost, GTT exit filled with shares left). **A failed delete keeps the id on the closed or cancelled ticket.** That is exactly the history the reverse check needs. |
| C. Expected open quantity | `position_quantity.protection_quantity(conn, row)`: the requested quantity while the entry has filled nothing (the GTT is placed with the order), otherwise the open quantity (filled minus what the exits sold). NULL if unknown, negative for NEEDS REVIEW. |
| D. Storage / dedup | `reconciliation_log(ticket_id, check_type ∈ {order_status, position_state}, expected, actual, resolved, detail)`. `_already_logged` dedups on (ticket_id, check_type, actual) over **unresolved** rows only, so after Mark Reviewed an unchanged finding returns next cycle (180 s). Never auto-resolved. |
| E. How alerts reach the UI | `GET /api/reconciliation/mismatches`, polled every 60 s. Every unresolved row becomes an **urgent** banner chip (`app.js` `buildAlerts`) and a line in `#reconciliation-list` with Mark Reviewed. The GTT watcher also broadcasts `trailing_error` for its own findings. |
| F. Manual Kite actions | A **deleted** app GTT is caught by the forward check and by the PK-7 watcher (position kept OPEN, `kite_gtt_id` → NULL, "PROTECTION MISSING"). A **resized** app GTT is caught by the PK-13 quantity check and resized back by the watcher. A **created** GTT, or one **left on a closed position**, was **not detected at all**. |

# New Reverse GTT Reconciliation

`backend/reconciliation.py`, `_check_broker_gtts` → `_classify_broker_gtts`, run every cycle after the forward check.

1. **Scope.** Only `status == "active"` GTTs, from the trading account's own session. A GTT whose Kite `user_id` is present and differs from the active session's `user_id` is skipped. A **BUY-side** GTT that no app ticket references is ignored: it cannot sell held shares, since it is the user's own buy order.
2. **References.** Every ticket of this account whose `kite_gtt_id` is set, excluding `mode = 'dry_run'`. Mode NULL (pre-mode rows) counts as live.
3. **Owning position.** A reference is an owner only if the ticket is `status = 'executed'`, not closed (`closed_at IS NULL`, and not closed by a pre-PK-8 exit), not an exit (`closes_ticket_id IS NULL`, not `execution.is_closing_sell`). That covers an entry still working, an open position, a partly sold one and an EXITING one.
4. **Classification.** Each active GTT gets exactly one outcome:

| Case | Outcome | Severity / suggested action |
|---|---|---|
| exactly one owner | **HEALTHY**: quantity and price belong to the forward check | — |
| more than one owner (same id on several positions) | `AMBIGUOUS_GTT_OWNERSHIP` | urgent / REVIEW POSITION |
| references, but no owner | `GTT_ON_CLOSED_POSITION` | urgent / CANCEL IN KITE |
| no reference, no open position in the symbol | `ORPHAN_GTT` | urgent / CANCEL IN KITE |
| no reference; every open position in the symbol already has its own active GTT | `MULTIPLE_ACTIVE_GTTS` | urgent / CANCEL IN KITE |
| no reference; exactly one unprotected position **and** this is the only unreferenced GTT on the symbol | `UNLINKED_POSSIBLE_GTT` | urgent / REVIEW POSITION |
| no reference; any other combination | `AMBIGUOUS_GTT_OWNERSHIP` | urgent / REVIEW POSITION |

Every row stores:

- `symbol`, `gtt_id`;
- `broker_quantity` (the GTT's quantity);
- `expected_quantity` (0 for a GTT that should not exist, the candidate's protection quantity for `UNLINKED_POSSIBLE_GTT`, NULL when ambiguous);
- `position_state`, `severity`, `suggested_action`;
- a detail naming the GTT type, side, quantity, trigger values and limit prices.

# GTT Ownership Rules

- **The exact `kite_gtt_id` is authoritative.** Symbol, quantity and trigger similarity are only a diagnostic: they decide between `UNLINKED_POSSIBLE_GTT` and `AMBIGUOUS_GTT_OWNERSHIP` and name the candidate. They never make a GTT "owned".
- **Reconciliation never writes `kite_gtt_id`.** Tests 13b/14 run three cycles and assert that it is still NULL. Mutation M6, which lets a same-symbol GTT count as the owner, is caught by 5 tests.
- **A GTT id on two open positions is ambiguous.** Neither is picked (test 14b).
- **Dry-run tickets** neither own a GTT nor are checked (test 22).

# Orphan GTT

An active SELL GTT that no app ticket references, on a symbol where the app has no open position. It is recorded with the GTT id, symbol, quantity, trigger and limit, `expected_quantity = 0`, `position_state = none` and `CANCEL IN KITE`, and raised as an urgent banner. Nothing is cancelled (test 3 asserts no `delete_gtt`, `modify_gtt`, `place_order` or `cancel_order` call, and that the GTT is still active).

**The user's own GTTs** on long-term holdings appear here too, because Kite GTTs carry no app tag. The user chose this. One Mark Reviewed per GTT silences it while it is unchanged (see Reconciliation Deduplication).

# Closed-Position GTT

`GTT_ON_CLOSED_POSITION` is for an active GTT whose only references are tickets that are no longer open:

- a position closed while `remove_protective_gtt`'s delete **failed** (test 10 drives the real `close_if_flat` with `delete_gtt` failing);
- a closed position still carrying its id (test 4);
- an entry cancelled or rejected by the broker whose GTT cleanup failed in `order_monitor` (test 4b, `position_state = "entry cancelled"`).

It is kept separate from `ORPHAN_GTT` because the app provably placed that GTT and still knows which ticket it belonged to. It is never treated as harmless (mutation M5 is caught by tests 4, 4b and 10).

A closed position whose `kite_gtt_id` was cleared while its GTT somehow stayed active has no reference left, so it is reported as `ORPHAN_GTT` (or `MULTIPLE_ACTIVE_GTTS` if the app holds the symbol again).

# Quantity Mismatch

This is the forward check (`_check_gtt_quantity`), now structured. Nothing is resized here: the GTT watcher's `_converge_protection` remains the single owner of resizing.

- **Expected** = `position_quantity.protection_quantity`, never `trade_tickets.quantity` (mutation M4 is caught by 5 tests).
- **Broker** = `max(leg quantities)`. An OCO is **one** GTT with two legs, and a mismatch on any leg is a mismatch.
- `GTT_QUANTITY_TOO_HIGH`: "it would try to sell N more share(s) than the position holds". Kite rejects an oversized CNC SELL, so the GTT may protect nothing, or it may sell shares held outside the app.
- `GTT_QUANTITY_TOO_LOW`: "N share(s) UNPROTECTED".
- `expected_quantity`, `broker_quantity` and the difference are stored and shown.
- The `actual` text (`GTT X covers N`) is unchanged, so existing unresolved rows keep deduplicating.

# Price Mismatch If Implemented

**Implemented, conservatively** (`_check_gtt_price`):

- The comparison is made only when the GTT has the structure the app placed:
  - a trailing position's `single` GTT: its trigger;
  - a fixed position's `two-leg` OCO: its stop trigger (the lower trigger of a long).
- Any other structure (a trailing ticket with an OCO, a missing trigger list) is not compared.
- Expected = `market.round_to_tick(current_stop_loss or stop_loss, tick)`, the same rounding execution, the ratchet and breakeven use. A difference of at most half a tick is not a mismatch (test: a 2900.02 stop against a 2900.0 trigger).
- **Two consecutive cycles** with the same (broker, expected) pair are required. The trailing ratchet calls `modify_gtt` a moment before it commits `current_stop_loss`, so one cycle can land in between (test: a transient difference is not flagged; mutation M10 is caught).
- The target leg is not compared. Nothing moves it after placement, and its limit is not buffered like the stop.

# EXITING Interaction

PK-8 keeps the original GTT active, and `closed_at` NULL, while an explicit exit is in flight. The position is therefore an owner, and its GTT is **HEALTHY** (test 9: an exit with `entry_fill_status = 'pending'`, two cycles, no finding). `position_state` reads `EXITING` (test 9b, via `execution.EXIT_IN_FLIGHT_SQL`).

Once the exit fills in full, `close_if_flat` stamps `closed_at` and deletes the GTT. If the delete fails, the GTT is `GTT_ON_CLOSED_POSITION` (test 10).

# PK-13 Partial Quantity Interaction

| Case | Expected | Test |
|---|---|---|
| entry requested 100, filled 40 (ended) | 40; a 40-share GTT is healthy | 7 |
| same, GTT still sized for the requested 100 | `TOO_HIGH`, 40 vs 100 | 7b |
| filled 40, a partial explicit exit sold 15 | 25; a 25-share GTT is healthy | 8 |
| same, GTT still covering 40 | `TOO_HIGH`, 25 vs 40 | 8b |
| entry still working with nothing filled | the requested quantity (the GTT is placed with the order) | existing PK-13 rule |

The shortfall check keeps using the PK-13 `open_quantity`. It never uses the requested quantity, and never counts a working entry's unfilled rest.

# Same-Symbol Multiple Positions

Two open TCS positions, each with its own GTT, are both healthy. If only B's GTT is oversized, only B is flagged: A's 40-share GTT is never compared with B's 25 shares (test 20).

An extra hand-made GTT on a symbol whose positions are all protected is `MULTIPLE_ACTIVE_GTTS`. It is never attributed to one of them (test 12). Two candidate GTTs for one unprotected position are both `AMBIGUOUS_GTT_OWNERSHIP` (test 13).

# Broker Snapshot / Performance

`BrokerSnapshot` makes every broker read once per cycle:

- **`get_gtts()`:** **1** call. It is now read every cycle, even with no open tickets, because the reverse check needs it: one extra call per 180 s when nothing is open.
- **`holdings()` and `positions()`:** 1 each. They are read lazily, only when the app has an open position to compare. Unchanged from before.
- **`order_history`:** per today's executed ticket, unchanged.

Test `test_one_broker_snapshot_per_cycle` asserts exactly one call each of `get_gtts`, `holdings` and `positions` with two positions and an orphan.

The indexes (`refs` by GTT id, open positions by (exchange, symbol), unreferenced GTTs by symbol) are built in memory from **one** SQL read of the account's tickets. No broker call is made per position or per GTT.

# Failure Handling

| Failure | Behaviour |
|---|---|
| `get_gtts()` raises | `snapshot.gtts = None`. The forward **and** reverse GTT checks are skipped, nothing is written and nothing is mutated. The cycle status reports `gtt_book: "unavailable"` with the error text, and the UI shows it in red. The failure is **never** converted to an empty list: that would mark every GTT missing and hide every orphan (mutation M7 is caught by test 15). |
| `holdings()` / `positions()` raise | Only the shortfall check is skipped (`holdings: "unavailable"`). The GTT checks still run (test 16). |
| no Kite session | Nothing runs; status `no_session`. |

Pending two-cycle sightings survive a cycle whose GTT book was unreadable. The next readable cycle compares against the last readable one.

# Reconciliation Deduplication

- **Forward and other findings: unchanged.** The key is (ticket_id, check_type, actual) over unresolved rows. After Mark Reviewed, a still-present identical finding returns next cycle (test 19c). This is right for a live position whose own protection is wrong, and it is what every existing check does.
- **Kite → app findings** (`ORPHAN_GTT`, `GTT_ON_CLOSED_POSITION`, `MULTIPLE_ACTIVE_GTTS`, `UNLINKED_POSSIBLE_GTT`, `AMBIGUOUS_GTT_OWNERSHIP`), the user's decision:
  - The key is (issue_code, gtt_id, actual) over **all** rows, reviewed ones included.
  - `actual` is a deterministic fingerprint: the GTT id, type, symbol, each leg's side, quantity and limit, the trigger values, and the ticket ids the finding is judged against.
  - Effects:
    - an unchanged finding is logged once (test 17);
    - a reviewed one stays silent while unchanged (test 19);
    - a quantity, trigger or leg change is a new row (tests 18 and 19);
    - the app opening a position in that symbol turns the same GTT into a different finding, for example `MULTIPLE_ACTIVE_GTTS` (test 19b).
- **Two-cycle confirmation for Kite → app findings.** The app's writes are not atomic with the broker:
  - an entry's GTT exists a moment before the confirming request stores `kite_gtt_id`;
  - `journal.close_position_for_exit` commits `closed_at` before `remove_protective_gtt` makes its two broker calls.

  A cycle landing in such a gap would otherwise raise an urgent false `ORPHAN_GTT` / `GTT_ON_CLOSED_POSITION`, and with review suppression that is the kind of alarm a user learns to ignore. A finding is therefore logged only when the **identical** fingerprint was seen on the previous readable cycle.
  - Tests: a gap GTT that gets linked is never flagged; a close-then-delete gap is never flagged; a changed fingerprint restarts confirmation. Mutation M12 is caught.
  - Cost: detection is about 3 minutes later for something that has usually existed for days.
  - The forward "GTT deleted in Kite" finding is **not** delayed.

# UI Changes

`frontend/index.html`, `app.js`, `style.css`. The Reconciliation panel gets:

- **A status line** from `GET /api/reconciliation/status`: "GTT book checked at HH:MM: N active GTT(s)". It reads in red "GTT book UNAVAILABLE at HH:MM — GTT checks did not run (error)" when the book could not be read, and likewise for holdings.
- **A "Protection / GTT" table**, shown when any GTT finding is unresolved. Columns: **Symbol · Issue · Expected qty · Broker GTT qty · GTT id · Position state · Severity · Suggested action · Mark Reviewed**. The full detail sits under the issue name.
- A note: nothing is cancelled or relinked automatically; cancel in Kite or in Orders & Trades → GTTs.
- **No cancel button.** Ownership of an unlinked GTT is by definition not proven, and the existing Orders & Trades GTT cancel (CSRF-protected) already exists for a deliberate human action.
- Other findings, and every pre-PK-16 row, stay in the existing list.
- The alert banner uses the row's severity (default urgent), and its chip scrolls to the GTT table for GTT findings.

`node --check frontend/app.js` passes. No browser run was made.

# Tests Added

`tests/test_gtt_reconciliation.py`: **43 tests**, driving `reconciliation._run_cycle()` against FakeKite with no sleeps.

| Brief # | Test(s) |
|---|---|
| 1 | `test_1_healthy_single_gtt_has_no_finding` |
| 2 | `test_2_healthy_oco_gtt_is_one_gtt_not_two` |
| 3 | `test_3_orphan_gtt_on_an_untraded_symbol_is_flagged_and_left_alone` |
| 4 | `test_4_closed_position_that_kept_its_gtt_is_flagged`, `test_4b_cancelled_entry_that_kept_its_gtt_is_flagged` |
| 5 | `test_5_oversized_gtt_shows_expected_actual_and_difference` |
| 6 | `test_6_undersized_gtt_names_the_unprotected_shares` |
| 7, 21 | `test_7_21_partially_filled_entry_expects_its_filled_quantity`, `test_7b_21_gtt_sized_for_the_requested_quantity_is_oversized` |
| 8 | `test_8_partial_exit_residual_is_the_expected_quantity`, `test_8b_gtt_still_covering_the_pre_exit_quantity_is_oversized` |
| 9 | `test_9_exiting_position_with_its_gtt_active_is_healthy`, `test_9b_position_state_reports_exiting` |
| 10 | `test_10_completed_exit_whose_gtt_delete_failed_is_flagged` |
| 11 | `test_11_manually_deleted_gtt_is_caught_by_the_forward_check` |
| 12 | `test_12_extra_manual_gtt_on_a_protected_position_is_multiple` |
| 13 | `test_13_two_candidate_gtts_for_an_unprotected_position_are_ambiguous` |
| 14 | `test_13b_14_single_plausible_gtt_is_reported_never_relinked`, `test_14b_same_gtt_id_on_two_positions_is_ambiguous` |
| 15 | `test_15_gtt_book_unavailable_writes_nothing_and_says_so` |
| 16 | `test_16_holdings_unavailable_gtt_checks_still_run` |
| 17 | `test_17_an_unchanged_finding_is_logged_once` |
| 18 | `test_18_a_changed_finding_gets_a_new_row` |
| 19 | `test_19_reviewed_orphan_stays_silent_until_it_changes`, `test_19b_…_when_the_app_opens_that_symbol`, `test_19c_forward_finding_review_keeps_existing_behaviour` |
| 20 | `test_20_same_symbol_positions_never_cross_link` |
| 22 | `test_22_dry_run_tickets_neither_own_nor_get_checked` |
| 23 | `test_23_another_accounts_gtt_is_ignored` (Kite's GTT `user_id` against the session's) |
| extra | one snapshot per cycle; two-cycle confirmation, the link gap, the close-then-delete gap and fingerprint restart; BUY-side and inactive GTTs ignored; price mismatch (single, OCO stop leg, sub-tick, transient); structured shortfall naming the armed GTT; the migration is idempotent; status before any cycle |

**Shared fixture:** `tests/conftest.py`'s `env` now calls `reconciliation.reset_cycle_memory()`. The two-cycle sightings are process memory, and FakeKite GTT ids and ticket ids restart in every test.

**No existing test was restated.** `test_partial_fills`, `test_live_safety`, `test_review_changes` (which calls `_check_position_state(conn, kite, account)` directly; the new `snapshot` argument is optional), `test_gtt_watcher`, `test_gtt_closure_proof`, `test_exit_lifecycle` and `test_baseline_api` pass unchanged.

# Mutation Results

`pk16_mutations.py` (scratchpad, not committed) ran once on the final code. For each mutant it:

1. applied one exact-anchor edit to `backend/reconciliation.py`, asserting the anchor was unique;
2. ran `test_gtt_reconciliation`, `test_partial_fills` and `test_live_safety`;
3. restored the original bytes.

| # | Mutation | Result |
|---|---|---|
| M1 | reverse broker → app scan disabled | **caught**, 19 |
| M2 | oversized quantity ignored | **caught**, 8 (incl. PK-13's `test_20_gtt_resize_failure…`) |
| M3 | undersized quantity ignored | **caught**, 1 (test 6) |
| M4 | requested quantity instead of the PK-13 protection quantity | **caught**, 5 (tests 7, 7b, 8, 8b, PK-13 test 20) |
| M5 | closed-position GTT treated as healthy | **caught**, 3 (tests 4, 4b, 10) |
| M6 | same-symbol GTT fuzzy-matched as its position's owner | **caught**, 5 (tests 9b, 12, 13, 13b, 19b) |
| M7 | `get_gtts` failure converted into an empty list | **caught**, 1 (test 15) |
| M8 | (extra) dry-run tickets may own a broker GTT | **caught**, 1 (test 22) |
| M9 | (extra) reviewed Kite → app findings not suppressed | **caught**, 1 (test 19) |
| M10 | (extra) price mismatch flagged on the first sighting | **caught**, 2 |
| M11 | (extra) another user's GTT not excluded | **caught**, 1 (test 23) |
| M12 | (extra) Kite → app finding logged on its first sighting | **caught**, 4 |

Restored = True; survivors: none. `reconciliation.py` SHA-256 is `34bbc28d2da9…` before and after. No code changed after the mutation run; only this report was written afterwards.

# Targeted Repeats

`pk16_repeats.py` (scratchpad) ran on the final code, after the mutation run.

- **PK-16 targeted suite ×10:** every run **354 passed**, 0 failed (113–116 s each). It covers:
  - `test_gtt_reconciliation`, `test_partial_fills`, `test_live_safety`;
  - `test_gtt_watcher`, `test_gtt_closure_proof`, `test_exit_lifecycle`;
  - `test_review_changes`, `test_baseline_api`.
- **Ambiguity / same-symbol subset ×20:** every run **7 passed**, 0 failed (about 3 s each). It runs `-k "ambiguous or same_symbol or multiple or unlinked or cross_link or fuzzy or relinked or never_flagged"` over the new file:
  - tests 12, 13, 13b/14, 14b and 20;
  - the link-gap and close-then-delete-gap tests.

No flakes were seen. PK-16 adds no threads or locks. The only cross-cycle state is the in-memory sightings, reset per test by the `env` fixture.

# Full Suite ×3

`full_x3.py` (scratchpad, `-rfEX`): three consecutive runs on the final code.

| Run | passed | xfailed | failed | xpassed | time |
|---|---|---|---|---|---|
| 1 | 761 | 0 | 0 | 0 | 232.9 s |
| 2 | 761 | 0 | 0 | 0 | 234.7 s |
| 3 | 761 | 0 | 0 | 0 | 242.9 s |

761 = 718 (baseline) + 43 (PK-16). No existing test was restated or removed.

# Database Validation

**Schema change.** `db._ensure_reconciliation_issue_columns` is called from `init_db()`. It adds eight nullable columns to `reconciliation_log` with `ALTER TABLE ADD COLUMN`, each guarded by `_column_exists`:

- `issue_code`, `severity`, `symbol`, `gtt_id`;
- `expected_quantity`, `broker_quantity`, `position_state`, `suggested_action`.

It also adds `idx_reconciliation_log_issue (issue_code, gtt_id)`.

`check_type` keeps its two-value CHECK, so **no table rebuild** was needed. GTT findings stay `order_status` (the existing convention) and the shortfall stays `position_state`. Existing rows keep NULLs, and the UI falls back to their detail.

**Validation.** `db_check.py` (scratchpad) made an online-backup copy of `kite_terminal.db.pre_pk16_bak` (opened `immutable=1`) in the scratchpad. It pointed `db.DB_PATH` at the copy, used a throwaway encryption key and `.env`, and ran `init_db()` **twice**.

| | before | run 1 | run 2 |
|---|---|---|---|
| `integrity_check` | ok | ok | ok |
| `foreign_key_check` violations | 0 | 0 | 0 |
| schema hash | `a1c0d580eae1` | `008d05ba4fd7` | `008d05ba4fd7` |
| `reconciliation_log` columns | 9 | 17 | 17 |

- Run 2 is **identical** to run 1: schema, every table's row count, and the indexes.
- **Row counts are preserved:** 18 tickets, 49 events, 50,556 OHLC rows, 10,133 instruments, 0 reconciliation rows.
- The only count differences before → run 1 are `account_safety_state` (+1 seeded row) and the empty `broker_failure_log`. These are **PK-12's own pending migrations**: the live DB has not yet been started under PK-12+ code. The PK-13 and PK-14/15 reports noted the same.
- **Live DB untouched:** SHA-256 `1378f8b47cd7…` and mtime unchanged.

# Known Limitations

1. **Kite payload details were not verified against a live account.** No live Kite call was made. Three things come from Kite Connect's documentation and the payload shape the codebase already relies on:
   - the GTT `user_id` field;
   - `type` values `single` / `two-leg`;
   - `status` values.

   If a real payload has no `user_id`, the account filter simply does not apply: every GTT in this session's book is this account's anyway.
2. **The user's own SELL GTTs are urgent orphans until reviewed** (the user's choice). There is no way to mark "this GTT is mine, forever". A reviewed GTT re-alerts when it is modified.
3. **Two-cycle confirmation** delays Kite → app and price findings by one cycle (about 3 min).
4. **Historical links that were cleared are lost.** The app clears `kite_gtt_id` when a GTT is spent or deleted. If such a GTT is somehow active again, it is an `ORPHAN_GTT`, not a `GTT_ON_CLOSED_POSITION`. The ticket's events still name it.
5. **The shortfall check is per symbol, not per exchange** (unchanged). A GTT is keyed by (exchange, symbol) for ownership.
6. **The price check skips unexpected structures** instead of flagging them, and it does not compare the OCO target leg.
7. **Findings are never auto-resolved** (unchanged). A fixed orphan's row stays unresolved until reviewed. The status line and a fresh cycle show whether it still exists.
8. **Manual SELL (short) tickets** own their BUY-side GTT like any entry. The PK-17 restriction is not started.

# Multi-Account Future Compatibility

Nothing multi-account was built. The assumptions:

- Reconciliation runs only for the **default account, and only if it is the trading account** (unchanged). It reads the GTT book, holdings and positions through **that account's own Kite session**, and compares them only with **that account's** tickets (`account_id = ?` on every query).
- A GTT whose Kite `user_id` differs from that session's user is ignored, never judged against this account's tickets.
- In the planned 3-account setup (1 trading, 2 investment-only), the investment accounts have no app tickets and no app-placed GTTs. Their GTTs are not this reconciliation's business. A future family-holdings view must **not** feed this check, or every family GTT would read as an orphan.
- `reconciliation_log` has no `account_id` column (unchanged). With a second trading account it would need one. That is recorded here, not built.

# Deferred Work

These are dependencies only; nothing was implemented.

- **PK-17:** the manual BUY+CNC restriction.
- **PK-18:** security and cloud hardening.
- **PK-19:** lifecycle UI redesign.
- **PK-20** to **PK-28**, and the multi-account family portfolio.
- **Possible follow-ups:**
  - an "acknowledge as my own GTT" state that persists across GTT modifications;
  - a guarded one-click cancel for `GTT_ON_CLOSED_POSITION` only (the one case where the app provably placed the GTT);
  - an `account_id` on `reconciliation_log`;
  - one real `get_gtts()` payload to confirm `user_id` / `type`.

# Deviations From Audit

1. **Detection only, no one-click cancel.** The audit's correction said "shown with a one-click cancel". The brief preferred detection-only, and an unlinked GTT's ownership is by definition unproven. The UI points to Kite and the existing Orders & Trades GTT cancel.
2. **More outcomes than "orphan / oversized".** The audit named two checks. PK-16 separates orphan, closed-position, multiple, unlinked-possible and ambiguous. It also covers **undersized** and price, because each needs a different human action.
3. **Two-cycle confirmation** for Kite → app findings. This was not in the audit. It was found while tracing the close path (`closed_at` committed before the GTT delete) and the entry path (GTT placed before `kite_gtt_id` is stored).
4. **Review suppression for Kite → app findings.** Existing checks re-raise a reviewed unchanged finding every cycle. The user chose silence until change for these.
5. **Structured columns rather than new `check_type` values.** The brief suggested new check types. `check_type` has a CHECK constraint, so widening it would need a table rebuild. An `issue_code` column gives the same structure with an idempotent `ADD COLUMN`.
