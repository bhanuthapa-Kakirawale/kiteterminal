# OPUS55 PK-13 Implementation Report: broker partial fills and open quantity (P1-3)

| | |
|---|---|
| Packet | PK-13 only (audit **P1-3**). PK-14 and later packets were not started. |
| Baseline before | **643 passed**, 0 xfailed, 0 failed, 0 xpassed. Re-measured on the untouched code before the first edit. |
| Result after | **669 passed, 0 xfailed, 0 failed, 0 xpassed** (see Full Suite ×3). 669 = 643 + 26 new tests in `tests/test_partial_fills.py`; the restated tests keep their counts. |
| Backups taken first | Tag `backup-pre-pk13` and branch `backup/pre-pk13`, both at `fc2b9da` (the tree was clean, so no worktree snapshot was needed). `backend/kite_terminal.db.pre_pk13_bak` was taken with the SQLite online-backup API: `integrity_check` = ok, 18 tickets. |
| Live DB | Untouched. SHA-256 `1378f8b4…` is identical before and after; tests use a per-test temporary DB and FakeKite; the migration was validated on a scratch copy. |
| User decisions | (a) New-entry risk limits count the **requested** quantity while an entry order is still working, and the filled quantity once it has ended. (b) The journal writes **one row per round trip**, at full close, at the quantity-weighted average exit price. (c) Emergency Exit on a working, partly filled entry **cancels the remainder, refreshes the fill from Kite, and sells the broker-confirmed quantity**. |

# Executive Summary

Before PK-13, `trade_tickets.quantity` — the **requested** order size — was used as if it were the position everywhere:

- the protective GTT legs, at placement and on every trailing or breakeven modification;
- open risk and same-day stock exposure;
- Emergency Exit, strategy-exit and alert-action exit sizing;
- the PK-4 "exit quantity must equal the position" rule;
- reconciliation's expected holding;
- the journal quantity.

A partial fill was only flagged:

- An entry that ended CANCELLED after 40/100 became `partial`, with an urgent "reduce the GTT to 40 in Kite".
- A partial explicit exit blocked the position until a human intervened (PK-8).
- A partial GTT sell kept the full 100 open (PK-7).

The result was an oversized SELL GTT, which Kite rejects for CNC beyond the holding, so the position was effectively unprotected. It also meant oversized exits and wrong P&L.

**PK-13 separates three quantities** and derives everything position-related from them:

| Quantity | Meaning | Where |
|---|---|---|
| `quantity` | order intent: what was requested | unchanged column |
| `filled_quantity` | broker-confirmed cumulative fill of that ticket's order, recorded **monotonically** | new column, with the invariant `0 ≤ filled_quantity ≤ quantity` |
| open quantity | entry `filled_quantity` minus what its executed exits sold | `backend/position_quantity.py`, the one place this arithmetic lives |

**What the terminal now does:**

- It records every fill it observes: progressive entry fills, partial-then-cancelled entries, progressive and partial exits, and partial GTT sells.
- It **resizes the protective GTT to exactly the shares held** (`modify_gtt`, quantity only) whenever that changes.
- It raises an urgent **PROTECTION QUANTITY MISMATCH** when it cannot resize the GTT.
- It sizes every exit and every Emergency Exit to the open quantity.
- It scales risk and exposure to the shares actually held, or that can still fill.
- It reconciles GTT quantity, recorded fills and holdings against the open quantity.
- It journals one round trip, at the real quantity and the weighted exit price, once the position is flat.

**Validation:**

- 26 new tests;
- 9/9 mutations caught;
- targeted suite ×10 and race subset ×20, all green;
- full suite ×3: 669 passed each run;
- migration run twice on a scratch copy of the latest real backup.

# Files Changed

| File | ± | Change |
|---|---|---|
| `backend/position_quantity.py` | new, 396 lines | The quantity model. SQL fragments (`filled_sql`, `exits_filled_sql`, `open_quantity_sql`, `risk_quantity_sql`); `open_quantity`, `protection_quantity`, `record_fill` (monotonic compare-and-set); `sync_protection` (GTT resize under a per-position lock); `close_if_flat`; `remove_protective_gtt` (moved from `order_monitor`, unchanged behaviour). |
| `backend/db.py` | +87/−15 | `_ensure_filled_quantity_column`: the column, its CHECK, and a one-time backfill in a savepoint. `_ensure_single_exit_owner_index`: the predicate is relaxed to in-flight exits and recreated idempotently. `_ensure_closed_trades_one_per_position_index`. |
| `backend/order_monitor.py` | +207/−86 | Entry progress (`_entry_fill_in_progress`), entry COMPLETE and partial-ended paths, exit progress, exit partial-ended, `_finalize_exit` via `close_if_flat`, `_close_if_sold_out`, `_fill_exceeds_order`. Updated docstring. |
| `backend/trailing_engine.py` | +186/−56 | Exit-evidence matching on the open quantity. `_record_gtt_exit` records partial and full GTT fills as one synthetic exit per GTT order. `_handle_gtt_closure` closes only when flat. The ratchet runs under the protection lock with a fresh quantity. The `_converge_protection` pass. The in-flight owner definition. |
| `backend/risk_engine.py` | +71/−28 | Risk and exposure use `risk_quantity`. `list_open_positions` returns `open_quantity` and `risk_quantity`. `_CLOSED_BY_EXECUTED_EXIT` is narrowed to legacy exits. `linked_exit_problem` uses in-flight ownership and the open quantity. |
| `backend/journal.py` | +36/−4 | `record_closed_trade` aggregates the round trip (filled quantity, weighted exit, scaled risk) and refuses an open position. The backfill picks up only closed positions, once each. |
| `backend/routes/tickets.py` | +63/−16 | An atomic exit-claim predicate (position open and `quantity == open quantity`), `_refuse_exit_claim`, `filled_quantity` set at acknowledgement or simulation, the in-flight definition, the alert-action link on the open quantity, and `filled_quantity`/`open_quantity`/`remaining_quantity` in ticket payloads. |
| `backend/routes/kill_switch.py` | +45/−9 | Sized to the open quantity. A working entry is cancelled, its fill refreshed from Kite, and the confirmed shares exited. New `QUANTITY_UNKNOWN` / `NOTHING_HELD` refusals. The preview shows `filled_quantity` and `open_quantity`. |
| `backend/reconciliation.py` | +49/−10 | A filled-quantity mismatch on `partial` tickets, the GTT OVERSIZED/UNDERSIZED check, and the position-state check on the open quantity. |
| `backend/strategy_engine.py` | +39/−25 | Exit tickets are sized to the open quantity; the scanner skips unknown or ≤ 0 quantities; the in-flight pending-exit definition; the breakeven GTT move runs under the lock with the fresh protected quantity. |
| `backend/execution.py` | +23/−8 | `EXIT_IN_FLIGHT_SQL`; `exit_state` docstring; the lifecycle comment. |
| `frontend/app.js` | +35/−6 | Quantity cells, PARTIAL FILL / PARTIAL EXIT badges, flagged rows, the Emergency Exit preview (see UI Changes). |
| `tests/test_partial_fills.py` | new, 26 tests | See Tests Added. |
| `tests/test_live_safety.py`, `test_exit_lifecycle.py`, `test_gtt_closure_proof.py`, `test_cancel_order.py` | small | Tests that encoded the PK-7/PK-8 interim partial rules, restated (see Tests Added). |
| `tests/test_gtt_watcher.py` | +5/−2 | An unrelated clock-dependent date fixed (see Deviations From Audit, item 9). |

The following were **not** changed:

- `tests/fake_kite.py`: its progressive `set_order_state`, partial `trigger_gtt`, quantity-changing `modify_gtt` and `raise_on_next` were already enough;
- `docs/OPUS55_AUDIT_REPORT.md`;
- sector mapping, T+1 holdings and the exposure basis (PK-14/15).

# Previous Quantity Semantics

This was traced in the current code before any edit. Each use of quantity is classified as **A** (order intent), **B** (position quantity), **C** (exit quantity) or **D** (GTT protection quantity).

| Use | Class | Before | After |
|---|---|---|---|
| `execution._execute_live` `place_order` | A | requested | requested (unchanged) |
| `execution._execute_live` initial GTT | D | requested | requested: placed with the order, before any fill; resized on the first fill |
| `execution._execute_exit` `place_order` | C | exit ticket `quantity` | unchanged; the ticket is now sized to the open quantity |
| `compute_risk_metrics` at creation; PK-12B notional; `signal_chart` | A | requested | unchanged (intent) |
| `order_monitor._matches_ticket` (adoption) | A | requested | unchanged: the order's own quantity |
| `order_monitor._resolve_entry` / `_resolve_exit` | B | fill only in messages | `filled_quantity` recorded; drives protection and closure |
| `trailing_engine._try_ratchet`, breakeven `modify_gtt` | D | requested | protection quantity, read fresh under the lock |
| `trailing_engine._is_exit_order_for` / `_classify` / synthetic exit | B/C | requested | open quantity plus what the same order already sold, or the GTT leg quantity |
| `risk_engine._open_risk_sum` | B | `risk_amount` (requested) | `risk_amount × risk_quantity / quantity` |
| `risk_engine._symbol_exposure` (same-day tickets) | B | `entry_price × quantity` | `entry_price × risk_quantity` |
| `risk_engine.linked_exit_problem` | C | exit == requested | exit == open quantity |
| `kill_switch._create_emergency_exit_ticket` | C | requested | open quantity |
| `strategy_engine._create_exit_ticket` | C | requested | open quantity |
| `routes/tickets.create_alert_action_sell` link | C | requested | open quantity |
| `reconciliation._check_position_state` | B | requested | open quantity |
| `reconciliation._check_order_status` | B | filled vs requested | also recorded filled vs broker for `partial` tickets |
| `journal.record_closed_trade` | B | buy requested quantity | Σ exit fills (= entry fill) |

Broker sources used: an order's `filled_quantity`, `quantity`, `average_price` and `status` from `orders()` and `order_history()`, and a GTT's `orders[].quantity` from `get_gtts()`. `pending_quantity` is not needed: the model uses the cumulative fill.

# New Requested/Filled/Open Quantity Model

- **`filled_quantity`**, on every ticket that reaches the broker:
  - entry: shares bought so far;
  - exit ticket: shares that exit has sold so far;
  - dry run: `= quantity` at simulation;
  - live acknowledgement or adoption: `0`;
  - NULL: unknown.
- **Open quantity** = entry filled − Σ `filled_quantity` of the position's executed exits. EXITING exits count what they have filled so far; a synthetic GTT exit counts too.
  - NULL if any input is unknown.
  - For a row written without the column, `filled_sql` applies the migration's backfill rule.
- **Risk quantity** (new-entry limits only): requested − sold while the entry order is `pending` or `unconfirmed`; open quantity once it has ended. Unknown counts as requested. Never below 0.
- **Protection quantity:** requested while the entry is still working and has filled nothing; otherwise the open quantity.
- **Ownership (PK-4), relaxed.** An exit owns its position only while its order is **in flight**: `confirmed`, or `executed` + `pending`/`unconfirmed` (`execution.EXIT_IN_FLIGHT_SQL`, the same predicate as the unique index). An exit that ended (`filled`, or `partial` after a partial fill) no longer blocks a follow-up exit for the remainder.
- **Closure.** A position closes only when its entry has ended (`filled`, `partial` or legacy) and its open quantity is exactly 0 (`close_if_flat`).
  - Open quantity < 0 is **never** closed: POSITION QUANTITY NEEDS REVIEW, and nothing is sent.
  - Unknown is never closed either.
- **Status vocabulary.** No new status or `entry_fill_status` value was added. An entry that ended after a partial fill keeps `entry_fill_status='partial'`, which now means "a real open position of the filled shares".

# Database Migration

`_ensure_filled_quantity_column` runs after `_ensure_submission_tracking_columns`, because the alert_action rebuild copies an explicit column list.

- **The column:** `ALTER TABLE trade_tickets ADD COLUMN filled_quantity INTEGER CHECK (filled_quantity IS NULL OR (filled_quantity >= 0 AND filled_quantity <= quantity))`.
  - SQLite 3.50 accepts the CHECK in ADD COLUMN, so no table rebuild was needed.
  - The migration test proves that the database refuses `filled_quantity > quantity`.
- **One-time backfill,** in the same `SAVEPOINT` as the ALTER. Python's sqlite3 opens no transaction for DDL, so the savepoint makes the column and its backfill land together or not at all.

  | Existing row | `filled_quantity` |
  |---|---|
  | `executed` and (`mode='dry_run'` or `entry_fill_status` NULL or `'filled'`) | `quantity`: the fill is proven, or predates fill tracking |
  | `executed`, live, `entry_fill_status='pending'` | `0` |
  | historical `partial` / `unconfirmed`, `failed`, `cancelled`, drafts, pending | NULL: **not guessed** |

- **Idempotency.** A second run finds the column and does nothing.
- **Owner index.** `idx_trade_tickets_one_exit_owner` is recreated with the in-flight predicate, but only when the stored SQL lacks it. The PK-4 "duplicates → warn, skip" guard is kept.
- **New index.** `idx_closed_trades_buy_ticket` (unique `buy_ticket_id`) is the storage backstop for one journal row per round trip, with the same warn-and-skip guard.
- **The live DB** holds no partial or unconfirmed rows (checked read-only before the backup), so nothing in it is ambiguous.

# Entry Partial Fills

- **Working entry, `filled_quantity` > 0** (`_entry_fill_in_progress`):
  - `record_fill`, a monotonic compare-and-set;
  - on an increase: `sync_protection` resizes the GTT to the fill;
  - one warning per distinct fill: "PARTIAL FILL — entry order … OPEN with 40/100 filled; the position holds 40 share(s) so far … Protective GTT … resized from 100 to 40".
- **COMPLETE.** The claim runs first (still the monitor's first write, the lock between racing writers), then `record_fill`.
  - The GTT is resynced **only** if an earlier partial had resized it, or the broker filled less than ordered.
  - A fill straight to COMPLETE makes no extra broker call (test 23 asserts no `get_gtts` or `modify_gtt`).
- **A broker figure above the order** (for example 120/100) is never recorded. It raises an urgent **BROKER FILL NEEDS REVIEW** (a de-duplicated `reconciliation_log` row), and no GTT change is made (test 21).

# Progressive Fills

10 → 25 → 40 is recorded monotonically. The GTT is resized 10 → 25 → 40, with one alert per step. The final COMPLETE for 100 resizes it to 100 (test 9/10).

A stale lower report arriving after a higher one (25 after 40) changes nothing: the compare-and-set only raises the figure (the stale-lower test, mutation M7).

# Entry Partial Then Cancel

The ticket stays **`executed`**, with `entry_fill_status='partial'` and `filled_quantity=40`, and `closed_at` NULL. The monitor never marks it `cancelled` or removes its GTT (mutation M4).

- **GTT:** resized to 40. The alert reads "entry order cancelled after a PARTIAL fill (40/100) … The position is OPEN with 40 share(s); the unfilled 60 will not be bought." Severity is warning, or urgent if the resize failed.
- **Every position operation** then sees 40: open positions, risk 400 (was 1000), exposure, Emergency Exit, reconciliation and the journal.
- **Edge case.** If every filled share was already sold while the order worked (an Emergency Exit sized to the fill so far), `_close_if_sold_out` closes the position when the entry ends.

# Explicit Exit Partial Fills

- **Working, 15/40 sold:**
  - the exit's `filled_quantity` becomes 15, so the open quantity is 25;
  - the GTT is resized to 25;
  - an urgent "PARTIAL EXIT … 15/40 filled. 25 share(s) … remain OPEN; nothing is journaled until the position is flat";
  - the exit stays EXITING and **still owns** the position, so Emergency Exit gives `already_exiting`.
- **Ended after the partial** (CANCELLED/REJECTED):
  - the claim moves the exit to `partial` and records 15 at the broker fill price and time;
  - 25 stay OPEN, and the GTT protects 25;
  - no journal row, and `closed_at` stays NULL;
  - the ended exit **no longer owns** the position: a new exit, including Emergency Exit, may sell the 25;
  - a stale sibling sized for 40 is refused `EXIT_QUANTITY_MISMATCH`, so it can never oversell;
  - nothing is resubmitted automatically.
- **COMPLETE for the full exit quantity:** `close_if_flat`.
  - **Flat:** exactly the PK-8 behaviour: `closed_at` at the fill, supersede, one journal row, GTT removed.
  - **Not flat** (the entry filled beyond what this exit was sized for): the position stays OPEN, the GTT is resized, and an urgent alert is raised.

# Protective GTT Partial Fills

This is the PK-7 residual.

- **Matching.** A GTT's order is this position's exit only if its quantity equals the open quantity plus what that same order has already been recorded as selling. For Kite's own GTT-to-order link, the GTT leg quantity is also accepted. `filled` means COMPLETE with every share of that order filled.
- **Partial evidence,** working or ended. `_record_gtt_exit` upserts **one synthetic exit per GTT order**, keyed by `matched_order_id`:
  - status `executed`, `entry_fill_status='partial'`;
  - `filled_quantity` and `filled_price` taken from the broker, monotonically;
  - no `kite_order_id`, so `order_monitor` never adopts it.
  - The open quantity drops 100 → 60, and risk, Emergency Exit and reconciliation follow. The position is not closed and not journaled.
  - While the order works, `kite_gtt_id` is kept and re-checked each cycle.
  - When it ends, `kite_gtt_id` is set to NULL and an urgent alert reads "60 share(s) are still held and UNPROTECTED … this app places no new stop by itself. Place a stop-loss for 60 in Kite."
- **Full evidence** (including after partials) upgrades the same synthetic exit to `filled`, then `close_if_flat`, which does **not** remove a GTT that fired itself. With no earlier partial, the outcome is the PK-7 closure, unchanged.
- **Race backstop.** The synthetic exit is inserted as in-flight (`pending`) first, and only then set to `partial`/`filled` in the same transaction. The PK-4 unique index therefore still refuses it if an explicit exit was claimed between the owner check and the insert.
- **Known orders.** An order this position's own earlier partial was attributed to stays its candidate; every other app-known order is still excluded.

# GTT Quantity Synchronization

`position_quantity.sync_protection(conn, kite, position_id, reason)` works as follows:

1. It takes the **per-position protection lock**. Every GTT modification of a position takes it: fill resizes, the trailing ratchet and the breakeven move. All run in this process.
2. It **re-reads the position inside the lock**, so whichever writer runs last uses the newest fill.
3. It reads the GTT from `get_gtts()`, or from the cycle's book when one is passed.
   - **Active, legs already equal the desired quantity:** nothing to do.
   - **Active, legs differ:** `modify_gtt` with the GTT's own `type`, `trigger_values`, `last_price` and leg prices, with **only the quantity changed**.
   - **Otherwise** — no GTT, a GTT not active, desired unknown or < 0, the book unreadable, or `modify_gtt` failing: **PROTECTION QUANTITY MISMATCH**. That is an urgent, de-duplicated `reconciliation_log` row, a ticket event and a broadcast. The position is always kept OPEN.

Self-healing works at two levels:

- **The GTT watcher's convergence pass.** It compares every active GTT's legs with the protection quantity using the book it already fetched, so there is no extra broker call while they agree. It resizes whatever a failed or raced resize left behind. Test 20: a failed resize is flagged, then healed on the next watcher cycle.
- **Reconciliation** flags GTT **OVERSIZED** (legs > held) or **UNDERSIZED** (legs < held) independently.

# Trailing Interaction

`_try_ratchet` now sends the **protection quantity**, read fresh under the protection lock, never `ticket["quantity"]`.

- Requested 100, filled 40: the ratchet moves the trigger to 335 with quantity **40** (test 6/7, mutation M9).
- Race test: an exit sells 15 and the resize to 25 lands after the watcher read the position but before it ratchets. The ratchet sends **25**, not the stale 40.

The breakeven confirm (`strategy_engine.confirm_stop_adjustment`) follows the same rule.

# Risk Engine Interaction

- **Open risk:** `Σ risk_amount × risk_quantity / quantity`. The factor is exactly 1.0 at the requested size, so the usual case is unchanged. Examples:
  - 40/100 working: 1000 (decision (a));
  - after the cancel: 400;
  - partial exit leaving 25 of 40: 2500;
  - GTT sold 40 of 100: 6000.
- **Same-day stock exposure:** `entry_price × risk_quantity`. The holdings, T+1 basis and sector are untouched (PK-14/15).
- **Max open positions** still counts positions, not shares.
- **`list_open_positions`** returns `open_quantity` and `risk_quantity` for its consumers (Emergency Exit, the exit scanner, reconciliation, the alert-action link).
- **`_CLOSED_BY_EXECUTED_EXIT`** is narrowed to legacy exits (`entry_fill_status IS NULL`). A `filled` exit that left shares no longer hides its position; every proven close since PK-8 stamps `closed_at`.
- **`linked_exit_problem`:**
  - closed means `closed_at`, a legacy exit, or flat with the entry ended;
  - in-flight owner → `EXIT_ALREADY_IN_PROGRESS` (not permanent);
  - unknown → `EXIT_QUANTITY_UNKNOWN`;
  - nothing held yet → `EXIT_NOTHING_OPEN`;
  - otherwise `quantity == open quantity`, else `EXIT_QUANTITY_MISMATCH`.

# Emergency Exit Interaction

- **Sizing.** Every exit ticket is sized to the position's open quantity, read at creation (mutation M2):
  - 100 requested, 40 filled → sells **40**;
  - 40 held after an ended partial exit of 15 → sells **25**.
- **Working entry** (decision (c)):
  1. request the cancel of the unfilled remainder;
  2. **refresh the fill from `order_history`** (a monotonic record, plus a GTT resize);
  3. exit the confirmed open quantity (`exit_submitted`, noting the cancel);
  4. if nothing has filled, it is `entry_cancelled`, as before.

  Test 5b: 25 recorded, 40 filled since the last monitor cycle → the SELL is 40.
- **Unknown or ≤ 0 open quantity** → `refused` (`QUANTITY_UNKNOWN` / `NOTHING_HELD`), and nothing is sent (test 22). This includes a working entry whose cancel failed and whose fill cannot be confirmed. Before PK-13 that case sent a full-quantity market SELL.
- **Ownership.** `already_exiting` applies only while an exit is in flight.
- **Atomic claim** (`routes/tickets._exit_claim_condition`). The claiming UPDATE of a linked exit also requires the position to be open and `quantity = open quantity`. An exit whose position changed between its check and its claim is refused (409 `EXIT_QUANTITY_MISMATCH`) and never reaches the broker. This is the Emergency-Exit-versus-fill race test, mutation M8.
- **Preview** shows `quantity`, `filled_quantity` and `open_quantity`.

# Reconciliation

| Check | Detects |
|---|---|
| `_check_order_status` | also today's `partial` tickets: broker `filled_quantity` ≠ recorded → **TICKET FILLED QUANTITY MISMATCH** |
| `_check_gtt_state` → `_check_gtt_quantity` | active GTT legs ≠ protection quantity → **PROTECTION QUANTITY MISMATCH**, OVERSIZED (GTT > open) or UNDERSIZED (GTT < open) |
| `_check_position_state` | expected holding = Σ **open** quantity: partial-exit residuals and partial entries included, working entries counting only their fill; a shortfall is flagged |
| `order_monitor` / GTT watcher | fill above the order; more sold than bought (open < 0); unknown open quantity: all NEEDS REVIEW |

Orphan-GTT reverse reconciliation (PK-16) was not attempted.

# Journal Semantics

This is decision (b): **one `closed_trades` row per round trip, written once the position is flat.**

`record_closed_trade(sell)`:

1. refuses an open position (`closed_at` NULL);
2. is a no-op if the position is already journaled (new unique `buy_ticket_id` index);
3. computes:
   - **quantity** = Σ exit `filled_quantity`, which must equal the entry `filled_quantity`; otherwise it refuses to journal a mismatched round trip;
   - **exit price** = the quantity-weighted average of every exit fill (a single exit uses its own price, so the result is byte-identical to before);
   - **risk_amount** scaled to filled / requested;
   - **sell ticket and exit date** = the closing exit's.

Examples:

- Requested 100, filled 40, sold 40 → the row is **40** shares, with risk 400 (test 8, mutation M6).
- 15 @ 3100 then 25 @ 3200 → one row: 40 shares @ 3162.5, P&L 6500 (the weighted-exit test).
- A partial exit, or a partial GTT fill, while shares remain → **no row** (tests 13 and 17–19).

`backfill_closed_trades` picks up only final exits of closed positions, once each.

# UI Changes

These are minimal (`frontend/app.js`); the PK-19 redesign was not started.

- **Quantity cell.** When quantities differ, the requested figure is followed by "Req 100 · Filled 40 · Open 40" for a position, or "Exit 40 · Filled 15 · Remaining 25" for an exit. It is unchanged for a normal full fill.
- **Badges:**
  - **PARTIAL FILL · 40/100 · working** for an entry still working;
  - **PARTIAL FILL · 40/100** for an ended partial entry;
  - **PARTIAL EXIT · 15/40 sold** for an ended partial exit;
  - **EXIT UNCONFIRMED — NEEDS REVIEW** for an unconfirmed exit;
  - EXITING and the PK-2 badges are unchanged.
- **Row flag.** A live position whose fill differs from its request is row-flagged.
- **Alerts.** Urgent partial and mismatch states stay visible through the existing urgent chips, from `reconciliation_log` rows and `trailing_error`/urgent `order_update` broadcasts.
- **Emergency Exit preview.** The quantity column shows the **open** quantity ("of 100 ordered" when it differs). An unfilled entry reads "order still working — the unfilled rest will be cancelled".
- **Payloads.** Ticket payloads (list, detail, confirm) carry `filled_quantity`, `open_quantity` and `remaining_quantity`.
- **Checks.** `node --check frontend/app.js` passes. No browser run was made for this packet.

# Restart Behavior

Everything is in the database: `filled_quantity` on entries, exits and synthetic GTT exits, the ended-partial states, and the reconciliation findings. Nothing is held in memory apart from the protection locks, which are recreated on demand.

Test 27 checks restart behaviour:

1. A 40-share partial entry with a 15-share ended partial exit survives `init_db()` twice.
2. Both fills persist, the open quantity is 25 and the GTT covers 25.
3. The next monitor and watcher cycles change nothing (no extra `modify_gtt`, no journal row).
4. Emergency Exit then sells exactly 25.

The one-time backfill cannot re-run.

# Concurrency Safety

| Race | Protection | Test |
|---|---|---|
| Two writers record fills (25 and 40, both orders) | `record_fill` compare-and-set `WHERE filled_quantity IS NULL OR filled_quantity < new`: monotonic | `test_cumulative_fill_is_monotonic_across_racing_writers` (barrier) |
| A stale lower broker report after a higher one | the same compare-and-set; the GTT is untouched | `test_a_stale_lower_broker_report_never_shrinks_the_fill` |
| Two monitors resolving the same COMPLETE | the claim stays the **first** write; the fill is recorded after it (the reordering made during development avoids a same-thread lock-wait deadlock) | existing `test_p0_2_race_two_monitors_resolve_a_fill_exactly_once` |
| Trailing ratchet versus fill resize | per-position lock plus a fresh read inside it | `test_trailing_ratchet_racing_a_fill_resize_never_writes_the_stale_quantity` |
| Emergency Exit / exit approval versus a fill update | atomic claim predicate (position open, `quantity == open quantity`) | `test_emergency_exit_racing_a_fill_update_is_refused_atomically` |
| A GTT partial versus an explicit exit claim | synthetic exit inserted as in-flight, so the unique index refuses it | existing PK-7 J/K tests |

**Residual.** The protection lock is per process. The app runs as one process; a second one would still be caught by the watcher's convergence pass and by reconciliation's OVERSIZED/UNDERSIZED check.

# Tests Added

`tests/test_partial_fills.py`, 26 tests. There are no sleeps; barriers and FakeKite call hooks drive the races.

| Brief # | Test |
|---|---|
| 1 | `test_1_entry_open_with_40_of_100_filled_is_a_40_share_position` |
| 2, 3 | `test_2_3_entry_partial_then_cancelled_stays_an_open_40_share_position` |
| 4 | `test_4_risk_counts_requested_while_the_entry_works_and_filled_once_it_ended` |
| 5 | `test_5_emergency_exit_sells_the_40_held_not_the_100_ordered`, `test_5b_…working_partial_entry_cancels_the_rest_and_sells_the_fill` |
| 6, 7 | `test_6_7_trailing_ratchets_the_gtt_for_the_40_held` |
| 8 | `test_8_journal_of_the_partial_entry_uses_the_40_filled` |
| 9, 10 | `test_9_10_progressive_fills_grow_the_protection_step_by_step` |
| 11–16 | `test_11_to_16_partial_exit_leaves_25_open_and_protected`, `test_partial_exits_journal_one_round_trip_at_the_weighted_exit_price`, `test_an_exit_larger_than_the_open_quantity_is_refused` |
| 17–19 | `test_17_to_19_gtt_sell_partially_filled_leaves_60_open`, `test_gtt_partial_then_complete_closes_once` |
| 20 | `test_20_gtt_resize_failure_is_an_urgent_mismatch_and_heals_later` |
| 21 | `test_21_broker_fill_above_the_order_is_never_recorded` |
| 22 | `test_22_more_sold_than_bought_is_never_closed_or_sold_again` |
| 23–26 | `test_23_full_entry_fill_changes_nothing_about_protection`, `test_24_26_full_explicit_exit_closes_and_journals_the_whole_position`, `test_25_complete_gtt_close_still_works`, `test_dry_run_entry_is_filled_in_full_at_once` |
| 27 | `test_27_partial_state_survives_restart` |
| races | the 4 tests in Concurrency Safety |
| migration | `test_migration_backfills_only_certain_fills_and_is_idempotent`: an old-schema DB with seven row states; the backfill result; a second run identical; `integrity_check`, FK check; the CHECK refuses filled > quantity |

**Existing tests restated.** Each encoded a PK-7/PK-8 *interim* partial rule that PK-13 replaces, and each carries a comment. Nothing was weakened; every restated test asserts more than before.

| Test | Was | Now |
|---|---|---|
| `test_exit_lifecycle::test_f_partial_fill_then_ended_needs_review_and_sends_nothing` → `…_leaves_the_rest_open_protected_and_exitable` | partial exit owns the position forever, GTT still 5, Emergency Exit `already_exiting` | 2 recorded, GTT 3, not closed or journaled; the stale 5-share sibling is refused `EXIT_QUANTITY_MISMATCH`; Emergency Exit sells exactly 3 |
| `test_cancel_order::test_partially_filled_exit_cancel_is_not_reported_as_cancelled` | "still owns the position" | ownership released, filled 2, open 3, GTT 3 |
| `test_gtt_closure_proof::test_partial_fill_is_not_a_close…` (B) | no exit recorded | exactly one `partial` synthetic exit of the shares sold (2, then 3); still no close, journal, SELL or LTP |
| `test_gtt_closure_proof` L parity table, `partial` row | 0 exits | 1 (partial) |
| `test_gtt_closure_proof::test_gtt_firing_while_an_explicit_exit_owns…[executed]` | owner inserted as `executed` with NULL fill (PK-8 read that as EXITING) | owner inserted as a real EXITING exit (`pending`) |
| `test_live_safety::test_p0_6_triggered_gtt_without_fill…[partial]` | no synthetic exit | a synthetic exit only as `partial` with exactly the filled shares |
| `test_live_safety::test_p0_4_database_refuses_a_second_owning_exit` | index refuses `status='executed'` | index refuses an in-flight exit (`executed` + `pending`) |
| `test_live_safety::test_p0_4_exit_owner_index_migration…` | legacy duplicate `executed` exits | duplicate in-flight exits |

# Mutation Results

`pk13_mutations.py` (scratchpad, not committed) ran once on the final code. For each mutant it applied exact-anchor edits (each anchor unique), ran the 9 targeted files, restored the originals and verified SHA-256.

| # | Mutation | Result |
|---|---|---|
| M1 | risk and exposure use the requested quantity | **caught**, 3 failures |
| M2 | Emergency Exit sizes to the requested quantity | **caught**, 6 |
| M3 | GTT resize skipped | **caught**, 10 |
| M4 | partial-then-CANCELLED entry treated as no position | **caught**, 9 |
| M5 | partial exit treated as a full close | **caught**, 5 (including the restated PK-8 F test) |
| M6 | journal records the requested quantity | **caught**, 1 |
| M7 | stale lower fill overwrites a higher one | **caught**, 2 |
| M8 | atomic exit-claim guard removed (extra) | **caught**, 1 (the race test) |
| M9 | trailing ratchet uses the requested quantity (extra) | **caught**, 2 |

Restored = True; survivors: none. The hashes after restore were:

- `journal.py` `41048b2a5ece…`
- `order_monitor.py` `e3cbd221657a…`
- `position_quantity.py` `dd7ea5d75cd0…`
- `risk_engine.py` `4458df35bdc7…`
- `routes/kill_switch.py` `2ab6519fb69d…`
- `routes/tickets.py` `83523b85525c…`
- `trailing_engine.py` `1c8a992e1426…`

No code changed after the mutation run; only this report was written afterwards.

# Targeted Repeats

`pk13_repeats.py` (scratchpad) ran on the final code, after the mutation run.

- **Targeted PK-13 suite ×10.** It covers `test_partial_fills`, `test_exit_lifecycle`, `test_gtt_closure_proof`, `test_live_safety`, `test_cancel_order`, `test_gtt_watcher`, `test_emergency_exit_isolation`, `test_review_changes` and `test_baseline_api`. Every run gave **338 passed**, 0 failed (47.6–53.4 s).
- **Race subset ×20.** It is `-k "race or racing or concurrent or monotonic or stale_lower"` over `test_partial_fills`, `test_live_safety`, `test_account_safety_integration`, `test_pk9_pk11_cross` and `test_gtt_watcher`: the 4 PK-13 races plus the PK-1, PK-2, PK-11 and PK-12C races. Every run gave **19 passed**, 0 failed.
- **Durations.** No race test takes more than 0.35 s (`--durations`), so none relies on SQLite's busy-wait.

No flakes were seen. Synchronisation uses barriers and FakeKite call hooks; there are no sleeps.

The first attempt at the full suite during development appeared to stall past 10 minutes. It was not reproduced: every later run, with `faulthandler_timeout` armed, finished in normal time with no hang dump.

# Full Suite ×3

These are three runs on the final code, after the mutation run.

| Run | passed | xfailed | failed | xpassed | time |
|---|---|---|---|---|---|
| 1 | 669 | 0 | 0 | 0 | 198.2 s |
| 2 | 669 | 0 | 0 | 0 | 197.6 s |
| 3 | 669 | 0 | 0 | 0 | 196.2 s |

669 = 643 (baseline) + 26 (PK-13).

# Migration Validation

`migration_check.py` (scratchpad) made an online-backup copy of `kite_terminal.db.pre_pk13_bak` into the scratchpad. It pointed `db.DB_PATH` there, used a throwaway encryption key so the real `.env` is never written, and ran `init_db()` **twice**.

- **`integrity_check`:** ok before, after run 1 and after run 2. There were 0 foreign-key violations throughout.
- **Row counts** are preserved for every existing table: `trade_tickets` 18, `ohlc_daily` 50,556, `instrument_cache` 10,133, `closed_trades` 0; 29 tables.
- **The only "changes"** are `account_safety_state` (+1 seeded row) and the empty `broker_failure_log`. These are PK-12's own pending migrations: the live DB has not been started under PK-12 code yet. The live DB's `system_events` rebuild is PK-12's, not PK-13's.
- **Backfill.** Only the 2 `executed` dry-run tickets got a value (1 = their quantity). All 16 others (`cancelled` ×7, `failed` ×4, `pending_confirmation` ×3, `rejected`, `risk_rejected`) stay NULL. The live DB holds no partial, unconfirmed or pending rows, so nothing was ambiguous.
- **Indexes.** The owner index was recreated with the in-flight predicate; `idx_closed_trades_buy_ticket` was created.
- **Run 2** is identical to run 1 (schema and counts).
- **Live DB untouched:** SHA-256 `1378f8b47cd7…` and mtime unchanged.
- **Cleanup.** Read-only opens of the WAL-mode backup had left an empty `-wal` (0 bytes) and a `-shm` next to it. They were removed after checking, and the backup re-verified: `integrity_check` ok, 18 tickets.

# Known Limitations

1. **Partial realized P&L is journaled only at full close** (decision (b)). While shares remain, realized P&L from a partial sale is not in `closed_trades`, so the PK-12F daily-loss limit sees it only when the position is flat. The shares sold are recorded (`filled_quantity`, `filled_price`), so nothing is lost.
2. **No automatic re-protection.** When a GTT's sell order ends after a partial fill, the remaining shares are **UNPROTECTED** (urgent alert). The app does not place a new GTT by itself.
3. **Protection before the first fill.** The GTT is placed with the entry order, for the requested quantity, before any fill; it is resized on the first recorded fill. If the app is offline while further shares fill, the GTT under-protects until the monitor's next cycle. Reconciliation flags UNDERSIZED; the watcher's convergence pass does not cover working entries.
4. **A working entry fully sold before it ends** keeps its GTT at the last size until the entry order ends. Then `close_if_flat` closes the position and removes the GTT.
5. **Emergency Exit refuses** (`NOTHING_HELD`) a working entry whose cancel fails and whose fill Kite cannot confirm. Before PK-13 it sent a full-quantity market SELL. The app now only ever sells broker-confirmed shares; the message tells the user to check Kite.
6. **Historical partial or unconfirmed rows** have `filled_quantity` NULL (unknown). They cannot be exited from the app (`EXIT_QUANTITY_UNKNOWN`) or protected-sized, and are flagged. The live DB has none.
7. **Resize uses the GTT's stored `condition.last_price`.** If Kite rejects the modification, the result is an urgent mismatch, and the watcher retries every cycle, alerting once per state.
8. **No user-chosen partial exits.** An exit always closes whatever the position still holds.
9. **One protection lock per process.** The app is single-process; see Concurrency Safety.
10. **One exit leg per round trip in the stats.** The weighted-average row counts as one trade in win rate and expectancy, as intended. Per-leg timing is not in `closed_trades`; it is in the exit tickets.

# Deferred Work

These are dependencies only; nothing was implemented.

- **PK-14:** sector mapping.
- **PK-15:** exposure basis (holdings, T+1, external positions); PK-13 changed only the application-ticket quantity semantics.
- **PK-16:** orphan GTT reverse reconciliation. PK-13's OVERSIZED/UNDERSIZED check covers only GTTs linked to open positions.
- **PK-17:** the manual BUY+CNC restriction.
- **PK-18:** security and cloud hardening.
- **PK-19:** a lifecycle UI; the PK-13 quantity display is deliberately minimal.
- **PK-20:** a paper broker that could simulate partial fills.
- **PK-21:** backtest realism.
- **PK-22:** hygiene. This includes a holiday calendar and the clock-dependent test dates noted below.
- **Multi-account** portfolios.
- **Possible follow-ups:**
  - automatic re-protection of a residual after a GTT partial, which needs a design decision;
  - a per-leg partial-realization journal, if daily-loss accuracy for partial sales becomes important.

# Deviations From Audit

1. **The GTT is resized, not just flagged.** The audit allowed "modify_gtt to shrink the GTT, or at least block trailing and journal on partial". PK-13 resizes in both directions: it shrinks after exits or a partial end, and grows with progressive fills. When it cannot, it raises an urgent mismatch, and the watcher and reconciliation heal or flag it.
2. **The PK-4 owner index is relaxed** from `status IN ('confirmed','executed')` to in-flight exits only. The PK-4 comment anticipated this. The invariant against a second SELL is now: the index for in-flight exits, plus the atomic claim predicate (`quantity == open quantity`, position open) for everything else.
3. **`_CLOSED_BY_EXECUTED_EXIT` is narrowed** to legacy (NULL-fill) exits: a proven fill is not a close by itself any more.
4. **Journal:** one row per round trip at the weighted exit (decision (b)), rather than a row per partial leg.
5. **Risk counts the requested quantity while an entry works** (decision (a)). The brief's "risk uses 40" holds once the entry has ended.
6. **Emergency Exit** cancels, refreshes and sells the confirmed fill for a working entry (decision (c)). It refuses (`NOTHING_HELD`) rather than sending an unconfirmed full-quantity SELL.
7. **PK-8's interim `partial` exit semantics** ("keeps the position, needs review") and **PK-7's** ("no exit recorded") are replaced by the modelled behaviour. The tests encoding them were restated (see Tests Added).
8. **No new status or `entry_fill_status` value,** and no table rebuild: the column only.
9. **Unrelated test fix.** `test_gtt_watcher::test_rsi_mean_reversion_position_oco_closure_is_detected` hard-coded `signal_date: "2026-09-25"`. Once the 2026-09-28 session completed (15:30 IST today), PK-12E correctly refused it as stale, and it failed independently of PK-13; it had passed in this morning's baseline. It now uses `market.last_complete_session_date()`. `test_account_safety.py:584` has a fixed date too, but it is a unit test with its own pinned clock and is unaffected.
