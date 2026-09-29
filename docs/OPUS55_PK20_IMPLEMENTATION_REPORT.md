# OPUS55 PK-20 Implementation Report: realistic, persistent paper broker

| | |
|---|---|
| Packet | **PK-20** only (audit **P1-13**, plan row PK-20). PK-21 and later were not started. |
| Baseline before | **1,068 passed**, 0 failed, 0 xfailed, 0 xpassed — re-measured on the untouched code (`36c4260`) before the first edit. |
| Backups | git tag `backup-pre-pk20` and branch `backup/pre-pk20` at `36c4260`; `backend/kite_terminal.db.pre_pk20_bak` (the WAL file was empty, so the copy is complete). |
| After | **1,110 passed**, 0 failed, 0 xfailed, 0 xpassed, three consecutive times (1,068 + 42 new). |

# Executive Summary

DRY RUN used to mean "pretend it filled". It now means **simulated broker**.

- A dry-run approval places a **paper order** (`PAPER-O-<n>`) and a **paper protective GTT** (`PAPER-G-<n>`) at a DB-backed paper broker. Nothing is filled at approval.
- The **paper exchange** runs inside the existing order-monitor loop. It fills orders only against an **observed LTP**:
  - MARKET orders at the first observed price;
  - LIMIT orders only once the price reaches the limit;
  - fills are made worse by a transparent **5 bps** slippage.
- It also covers:
  - progressive **partial fills** through a persisted test/demo fill plan;
  - **DAY expiry** and **AMO** sessions;
  - **GTT triggers** that place a LIMIT exit order, which fills (or not) later.
- Every dry-run ticket then runs through the **same code** as a live one, with the paper broker in place of Kite:
  - the PK-2 submission tracking and UNKNOWN recovery;
  - the order monitor's fill/cancel/partial resolution (PK-8, PK-9, PK-13);
  - the GTT watcher (PK-7) and trailing ratchet;
  - PK-13 GTT resizing;
  - the PK-19 lifecycle.
- Paper state is fully persisted in the new `paper_orders` and `paper_gtts` tables and survives restarts.
- Live and paper are strictly isolated:
  - a single `check_mode` guard sits at every broker touch-point;
  - every live query is `mode='live'`;
  - dry run makes **zero** Kite order/GTT calls (it only reads LTPs).

Validation:
- the new PK-20 suite: **42 tests**, green ×10;
- the partial/restart/lifecycle subset: green ×20;
- **8/8 mutations caught**;
- migration run twice on a copy of the real DB, with no row changed versus the pre-PK-20 code;
- the real DB hash is unchanged;
- a browser run under enforced CSP: **29/29**;
- `e2e_ui.py` identical to the baseline (53/54, same pre-existing failure);
- the full suite green three consecutive times.

# Previous Dry-Run Limitations

The current flow was traced before any edit:

| | Before PK-20 |
|---|---|
| A. entry filled | `execution.execute_ticket` → `_simulate()` returned `entry_fill_status='filled'`. `_record_execution_result` stamped `filled_price = entry_price` (the **ticket's** price), `filled_quantity = quantity` and `filled_at = now`, at approval. |
| B. exit closed | The same `_simulate()`. `confirm_and_execute_ticket` then called `journal.close_position_for_exit` immediately, so the position was CLOSED and journaled at approval (the exact PK-8 violation, tolerated for dry run). |
| C. dry-run GTT | None. `kite_gtt_id` stayed NULL; `lifecycle._protection` returned `NOT_APPLICABLE`. |
| D. order IDs / status | None: `kite_order_id` NULL, `submission_state` NULL, no order tag. |
| E. order monitor | `pending_tickets` / `unresolved_submissions` were hard-filtered to `mode='live'`, as were `trailing_engine.monitored_positions` and `position_quantity.sync_protection`. Dry-run tickets were never seen. |
| F. P&L / journal | `closed_trades` row written at approval by `close_position_for_exit`, at the ticket's reference entry price and the exit ticket's reference price. Gross; there is no cost model anywhere in the journal. |

As a result, the PK-2, PK-7, PK-8, PK-9 and PK-13 machinery could only ever run for the first time with real money.

# Paper Broker Architecture

`backend/paper_broker.py` (new) contains:

- **`PaperBroker(conn, account_id, market_data=None)`**, a KiteConnect stand-in. It exposes exactly the subset the terminal uses:
  - `VARIETY_REGULAR`/`VARIETY_AMO` and `GTT_TYPE_SINGLE`/`GTT_TYPE_OCO` (Kite's own constant values);
  - `place_order`, `cancel_order`, `orders`, `order_history`, `trades`;
  - `place_gtt`, `modify_gtt`, `delete_gtt`, `get_gtts`;
  - `ltp`, delegated **read-only** to `market_data` (the real Kite client in production, FakeKite in tests);
  - `holdings`/`positions` (net paper fills; used only in the GTT watcher's "held" notes).

  `__getattr__` raises `PaperBrokerUnsupported` for anything else, so nothing can silently fall through to a live method.
- **`broker_for(mode, kite, conn, account_id)`**: Kite for live, a paper broker for dry run.
- **`check_mode(broker, mode)`**: raises `ModeIsolationError` if a dry-run ticket meets anything but the paper broker, or a live ticket meets the paper broker.
- **`evaluate(account_id, market_data, now=None)`**: the simulated exchange step.
- **`fill_price` / `is_fillable`**: the pure pricing rules.
- Test/demo hooks:
  - `set_fill_plan` (persisted);
  - `reject_order`;
  - `accept_then_raise` (in memory, one-shot);
  - `reset_test_hooks`.

**Transactions (a deliberate design choice).** A `PaperBroker` works on the **caller's connection and never commits**. There are two reasons:

1. **Atomicity.** A paper order is written in the same transaction as the ticket update that records its id. There is no window where the paper book has an order the app does not know about, other than the explicit PK-2 test hook.
2. **No SQLite writer deadlock.** The order monitor and GTT watcher call `modify_gtt`/`delete_gtt` while holding their own open write transaction (e.g. after `record_fill`). A second connection would block on that lock until the busy timeout.

`evaluate()` is the one entry point that opens its own connection and commits each step.

**Integration points.** No lifecycle code was forked; only the broker object and the mode filters changed.

| Module | Change |
|---|---|
| `execution.py` | `_simulate` deleted. `execute_ticket(broker, …)` runs `check_mode` first; a mismatch is a definite "refused before any broker call" failure. Dry-run entries and exits go through the unchanged `_execute_live` / `_execute_exit`. The result message is prefixed "PAPER (dry run — simulated broker, no real order):". |
| `routes/tickets.py` | `broker = paper_broker.broker_for(...)` on the request's connection. The claim records the order tag / `submitting` / `submitted_at` for **both** modes (PK-2). An execution exception is an UNKNOWN outcome in both modes. `_record_execution_result` has no dry-run branch: it never fills. **The confirm-time dry-run close/journal block is removed.** `start_trailing` also accepts a paper position. |
| `account_safety.py` | `precheck`: the PK-12H `decide_variety` (market hours / explicit AMO consent) now also applies to dry-run entries. The breaker, daily loss, daily entry count and notional checks stay live-only. |
| `order_monitor.py` | `pending_tickets` / `unresolved_submissions` take `mode`. `check_pending_orders(kite=None, mode="live")`; for `dry_run` the broker is a `PaperBroker` on the cycle's connection. New `check_paper_orders()` runs `evaluate` and then the dry-run cycle. `run_order_monitor_loop` runs it after the live cycle, each part with its own exception guard. |
| `trailing_engine.py` | `monitored_positions(conn, account, mode)`; `_tick_sync` runs live, then paper, through the same `_tick_mode`. `_other_positions_in_symbol` is mode-aware. |
| `position_quantity.py` | New `broker_tracked(position)`. `sync_protection` resizes a paper position's GTT through the paper broker (legacy dry-run rows are still refused). `check_mode` guards `sync_protection` and `remove_protective_gtt`. |
| `lifecycle.py` | `_protection` treats a paper position like a live one; `CANCEL_ORDER` is allowed for paper orders. New `paper` flag and `paper_order` (id, side, status, fill), read in **one** extra batched query of `paper_orders`. |
| `routes/orders.py` | `POST /api/orders/{id}/cancel` routes a `PAPER-` id to the paper broker (same PK-9 semantics). `_linked_ticket_map` and the GTT-cancel lookup are `mode='live'`. |
| `reconciliation.py` | The order-status check is `mode='live'` (it would otherwise have sent paper ids to Kite's `order_history`). |
| `db.py`, `config.py` | `_ensure_paper_tables` (see Database Migration); `PAPER_SLIPPAGE_BPS = 5.0`. |
| `frontend/lifecycle.js`, `app.js`, `style.css` | The minimal UI additions (see PK-19 Interaction). |
| `tests/demo_server.py` | Opt-in `--paper-controls` (demo-only paper routes; the paper exchange advances on demand) and `--market-open`. |
| `docs/USER_GUIDE.md` | The Dry Run paragraph describes paper orders, observed-price fills, slippage, AMO and gross P&L. |

# Paper Order Model

`paper_orders` has the following columns:

- identity and routing: `paper_order_id` (`PAPER-O-<id>`, UNIQUE), `account_id`, `ticket_id` (parsed from the `kt<id>x…` tag), `source_gtt_id` (for a GTT-placed order), `tag`;
- order terms: `variety`, `exchange`, `tradingsymbol`, `transaction_type`, `product`, `order_type`, `quantity` (requested), `filled_quantity`, `pending_quantity`, `price` (limit), `trigger_price`;
- fill and state: `average_price` (volume-weighted), `status`, `status_message`, `session_date` (DAY validity), `fill_plan` (test/demo hook);
- timestamps: `created_at`, `updated_at`, `filled_at`, `cancelled_at`.

CHECK constraints enforce `0 ≤ filled_quantity ≤ quantity`, the side, variety and order type. A partial UNIQUE index on `tag` makes a second order for the same ticket impossible. A conflict raises `GeneralException`, an UNKNOWN outcome, so PK-2 adoption resolves it; it can never be a duplicate.

**Order states (mapped to Kite's own strings, so the order monitor needed no new state):**

| Packet state | Stored / reported | Monitor meaning |
|---|---|---|
| OPEN | `OPEN` | working |
| PARTIALLY_FILLED | `OPEN` with `0 < filled_quantity < quantity` (exactly how Kite reports it); shown as **PARTIAL** in the UI | partial in progress |
| TRIGGER_PENDING | not used for orders: the terminal never places SL/SL-M (its stops are GTTs), so they are refused with `InputException`. An active GTT is the trigger-pending analogue. | — |
| (AMO) | `AMO REQ RECEIVED` until its session opens | working |
| COMPLETE / CANCELLED / REJECTED | the same | terminal |

`orders()` lists today's orders plus every order still working (an AMO waiting overnight). `order_history(id)` finds any paper order, so a paper order is never "unconfirmed".

# Paper GTT Model

`paper_gtts` has the following columns: `paper_gtt_id` (`PAPER-G-<id>`), `account_id`, `trigger_type` (`single` / `two-leg`), `exchange`, `tradingsymbol`, `trigger_values` (JSON), `last_price`, `legs` (JSON: side, **quantity**, LIMIT price and, once fired, the Kite-shaped `result`), `quantity`, `status` (`active`/`triggered`/`deleted`), `created_at`, `updated_at` and `triggered_at`.

- The GTT types the app uses are both supported: a **single** stop (trailing positions) and an **OCO** stop/target.
- `get_gtts()` returns the Kite shape (`id`, `type`, `status`, `condition{…}`, `orders[…]`).
- The trailing engine, PK-13 resizing and closure evidence therefore read it unchanged.

# Market Orders

- A paper MARKET order is placed `OPEN` and **never fills at placement**.
- The first `evaluate()` with an observed LTP fills it at `fill_price(side, "MARKET", ltp)`.
- The ticket's own price is never used.
- With no LTP (the quote call fails, or the symbol has none) the cycle is skipped: the order stays OPEN and no fill is invented.

Tests: 1–4 and `test_market_order_without_an_observed_price_stays_open`.

# Limit Orders

- BUY LIMIT fills only when `ltp ≤ limit`, at `min(limit, slipped ltp)`.
- SELL LIMIT fills only when `ltp ≥ limit`, at `max(limit, slipped ltp)`.
- A fill never goes past the limit.
- The rule is tested at unit level (paper broker plus `evaluate`) and end to end (a dry-run LIMIT entry, and a LIMIT exit ticket).
- A GTT's leg order is a LIMIT order, so a gap through a stop's limit leaves the exit **unfilled**. The GTT watcher then raises its existing "GTT TRIGGERED — EXIT NOT FILLED" alert (`test_gap_through_the_stop_limit_leaves_the_exit_unfilled`).

# Partial Fills

- **Runtime (simple rule):** at the first eligible price, the whole remaining quantity fills.
- **Deterministic hook:** `paper_broker.set_fill_plan(order_id, [10, 25, 40])` stores cumulative steps on the order. Each eligible evaluation advances one step, and the average price is volume-weighted.
  - Because it is persisted, a plan survives a restart (`test_fill_plan_survives_a_restart`).
  - It is available in the demo as `POST /api/demo/paper/fill-plan`.
- Partial entry 10 → 25 → 40:
  - ENTRY_PARTIAL, then ENTRY_PARTIAL, then OPEN;
  - open quantity 10 / 25 / 40;
  - the paper GTT resized 40 → 10 → 25 → 40 by the unchanged PK-13 path.
- Partial exit, 15 of 40:
  - EXIT_PARTIAL;
  - cancelling the rest leaves 25 OPEN, protected by a GTT resized to 25, and not journaled;
  - a second exit of 25 closes the position with **one** journal row.

# Slippage

`config.PAPER_SLIPPAGE_BPS = 5.0` (user-chosen default). The rule is transparent:

- **BUY** = observed × (1 + bps/10 000), rounded **up** to the tick.
- **SELL** = observed × (1 − bps/10 000), rounded **down** to the tick.
- A LIMIT order is never filled past its own limit.

Exact arithmetic is tested:

| Case | Result |
|---|---|
| 1500.00 BUY | 1500.75 |
| 1000.02 BUY | 1000.55 |
| 1600.00 SELL | 1599.20 |
| 1000.02 SELL | 999.50 |
| LIMIT caps | 1500.00 / 1600.00 |
| 0 bps | equals the LTP |

The browser run journaled 1588.80 in and 1619.15 out, from LTPs of 1588 and 1620.

# Cancellation

PK-9 semantics are unchanged:

1. `POST /api/orders/PAPER-O-n/cancel` asks the paper broker, which resolves the cancel at once (a compare-and-set).
2. The route only logs the request and never touches the ticket.
3. The next monitor cycle sees `CANCELLED` and resolves the ticket:
   - with nothing filled, the ticket is `cancelled` and its paper GTT deleted;
   - after a partial fill, the entry is `partial` with the filled shares open and the GTT resized.

Races are handled by the same compare-and-set:

- If the fill won, the cancel is refused (502, "cannot be cancelled — it is COMPLETE") and the ticket ends filled.
- The Positions view offers a direct **Cancel paper order** button, since paper orders are not on Kite's Orders page.

FakeKite's `cancel_order` is never called.

# Restart Safety

The paper broker holds **no process state**: everything is in `paper_orders` / `paper_gtts`, and ticket linkage is in `trade_tickets`. The one in-memory item is the one-shot test hook.

The tests re-run `db.init_db()` twice (the startup migration) and construct fresh brokers:

- an OPEN paper order survives and fills **once** (one paper order row, even after further cycles);
- an EXITING position survives, closes **once** and gets one journal row;
- a paper GTT survives and still protects;
- a fill plan survives.

The unique tag index is the storage backstop against a duplicate submission. Mutation M6 (re-inserting working orders at startup) is caught.

# Entry Lifecycle

BUY 40 MARKET in dry run:

1. After approval: ticket `executed` / `pending`, `submission_state='acknowledged'`, paper order OPEN, `filled_quantity 0`, paper OCO GTT for 40. Lifecycle **ENTRY_OPEN**, protection PROTECTED.
2. On price evidence: the paper order is COMPLETE 40 @ the observed price. The monitor records `filled`, the fill price and the fill time. Lifecycle **OPEN**.

Outside market hours, a dry-run entry needs explicit AMO consent exactly like a live one (PK-12H). With consent it becomes a paper AMO (`AMO REQ RECEIVED`) that waits for its session. A regular order still working when its session closes expires (DAY validity).

# Exit Lifecycle

PK-8 now applies to dry run:

1. An accepted paper SELL leaves the position **EXITING**: still open, still protected, not journaled.
2. Only the paper broker's full fill makes the monitor's `_finalize_exit` close it: `close_if_flat`, one journal row, and removal of the paper GTT.

The old instant close is gone; mutation M7 restores it and is caught. Legacy dry-run positions (from before PK-20, with no paper IDs) can also be exited this way: the exit becomes a paper SELL.

# GTT Lifecycle

1. When the observed price crosses a trigger, the GTT becomes `triggered` and places a regular LIMIT paper order for that leg: the stop leg at the buffered stop price, the target leg at the target. The order carries no tag, as with Kite, and is linked via `orders[leg].result.order_result.order_id`.
2. The trigger itself changes no position: in the tests the position is still **OPEN** after the trigger.
3. The order fills in a later evaluation under the LIMIT rule. The unchanged PK-7 watcher then records the synthesized exit ticket (mode `dry_run`), closes the position once flat, and journals it once.
4. A partially filled GTT order gives EXIT_PARTIAL with the residual still open (test 18).

# Trailing Interaction

The trailing engine is not forked: `_tick_mode` runs the same code with a `PaperBroker`.

- In the test, the target is touched (trailing activates), then the price rises to 1620.
- The ratchet calls `modify_gtt` on the paper broker. The paper GTT's trigger moves 1450 → 1600, the leg price becomes 1592.00 (buffered), the quantity stays 40, and `current_stop_loss` is 1600.
- FakeKite's `modify_gtt` is never called.
- A later fall through 1600 fires the moved stop and closes the position at the observed 1599.

# PK-13 Interaction

The paper broker reports `filled_quantity` exactly like Kite. `record_fill`, `sync_protection`, `open_quantity` and `close_if_flat` are used unchanged, and the paper GTT always covers the shares actually held:

- entry partials: 40 → 10 → 25 → 40;
- an exit partial: 40 → 25.

Mutation M8 (skip the paper resize) is caught.

# PK-19 Interaction

The server derives the lifecycle exactly as for live positions, with no frontend special-casing of states. The Positions view shows a paper trade moving through:

- ENTRY_OPEN → (ENTRY_PARTIAL) → OPEN → EXITING → (EXIT_PARTIAL) → CLOSED;
- PROTECTED protection for the paper GTT.

Additions:

- `paper` (bool) and `paper_order` {id, side, status OPEN/PARTIAL/COMPLETE/CANCELLED/REJECTED/AMO, fill}, from one constant extra query. The PK-19 query-count test still proves the count does not grow; its absolute bound went from 4 to 5 for this query.
- UI, minimal:
  - a "PAPER entry PAPER-O-3 · PARTIAL 25/40" line under the DRY RUN chip;
  - the **Cancel paper order** action;
  - "Paper Order ID / Paper GTT ID" labels in the detail dialog;
  - the approval note and toast now say PAPER / simulated broker;
  - the AMO checkbox also appears for dry-run entries while the market is closed.
- Emergency Exit stays live-only, as before.

# Live/Paper Isolation

- `check_mode` runs at `execute_ticket`, every monitor cycle, every watcher cycle, `sync_protection` and `remove_protective_gtt`.
- `PaperBroker` refuses non-`PAPER-` ids and raises on any unsimulated attribute.
- Every live query filters `mode='live'`. The dry-run watcher and monitor queries additionally require `PAPER-` ids, so a dry-run row carrying a stray Kite id is ignored by both books.
- The market-data read (LTP) is the only shared call.
- Live-only account controls are unaffected by paper activity: the daily live entry count, realized live P&L (daily loss), the broker circuit breaker and the notional limit (tested). The trading lock still blocks dry-run approvals (tested).

Tests prove:

- a full dry-run lifecycle makes **zero** FakeKite `place_order`, `cancel_order`, `place_gtt`, `modify_gtt` or `delete_gtt` calls, and leaves FakeKite's order and GTT books empty;
- the live monitor, reconciliation, `/api/orders` and `/api/gtts` never see a `PAPER-` id;
- each crossover is refused.

Mutation M5 (route dry run to Kite) is caught by 31 tests.

# Database Migration

`db._ensure_paper_tables` uses `CREATE TABLE/INDEX IF NOT EXISTS` only. It is idempotent and needs no table rebuild. `trade_tickets` is unchanged.

Validation, all on scratch copies (`migrate_one.py`, `migration_check.py` in the session scratchpad):

- **Twice on a copy of the real DB:** the schema after run 1 equals the schema after run 2.
- **Rows preserved:** a second copy of the real DB was migrated with the **pre-PK-20 code** (`git archive backup-pre-pk20`). Every one of the 31 tables was digested. The only differences are:
  - the two new empty tables and their 3 indexes;
  - `account_safety_state.updated_at`, a seed timestamp 2 s apart between the two runs.

  **No existing row changed.**
- The real DB was last migrated before PK-12, so its first start will also apply the pending PK-12..PK-19 migrations. This is unchanged by PK-20.
- **Real DB untouched:** `backend/kite_terminal.db` sha256 `1378f8b4…dacf` is the same before and after all work.
- **Pre-existing issue found, not fixed (out of scope):** on a *fresh* database, the second `init_db()` re-creates `idx_dashboard_panels_account_type`. `SCHEMA` creates it every run; the dashboard migration drops it only once. The PK-20 migration test therefore measures stability from the second run on. It is recorded for PK-22.

# Performance

- **No quote storm.** `evaluate()` makes **zero** market-data calls when nothing is working, and otherwise exactly **one batched** `ltp()` call per cycle for every symbol involved. Tested: four orders on three symbols make one call with three sorted keys.
- The paper cycle uses the existing order-monitor cadence (20 s); the watcher uses the trailing cadence (45 s).
- There are no busy loops, and every step is a short compare-and-set transaction.
- The PK-19 lifecycle adds one constant query.

# Tests Added

`tests/test_paper_broker.py`, **42 tests**, numbered by the packet's list:

- **MARKET 1–4:** approval fills nothing; the first observed price fills; the fill price is the observed one; exact slippage arithmetic; slippage in real fills and the journal; no price means no fill.
- **LIMIT 5–8:** BUY waits above its limit and fills at or below it, exactly at the limit too; SELL waits below and fills at or above; a LIMIT exit ticket closes only at its limit.
- **PARTIAL 9–12:** 10 → 25 → 40 entry with the GTT resized; a 15/40 exit leaves 25, then cancel → 25 open and protected → a second exit → CLOSED, journaled once.
- **GTT 13–18:** OCO created sized to the order; the stop trigger places a SELL LIMIT; the OCO target leg; the trigger alone does not close; the fill closes and journals once; gap-through stays unfilled; a partial GTT exit leaves a residual.
- **TRAILING 19:** the ratchet modifies the paper GTT; FakeKite is untouched.
- **CANCEL 20–21:** cancel OPEN → monitor → CANCELLED with the GTT deleted; the fill wins the race; cancel after a partial keeps the filled shares; cancel of an unknown paper id.
- **RESTART 22–24:** open order, EXITING, GTT and fill plan survive; migration idempotency.
- **ISOLATION 25–26:** a whole lifecycle with zero live calls; the crossover guards; live paths blind to paper; paper does not touch live account controls; the trading lock still blocks dry run.
- **LIFECYCLE 27–31:** the Positions API walks ENTRY_OPEN → ENTRY_PARTIAL → OPEN → EXITING → CLOSED; a legacy dry-run position is `NOT_APPLICABLE` yet exits through paper.
- **Extras:**
  - AMO waits for the next session (refused without consent);
  - session placement rules;
  - DAY expiry;
  - market closed means no fills, triggers or LTP calls;
  - PK-2 "accepted, response lost" → UNKNOWN → adopted by tag, never resubmitted;
  - duplicate tag refused;
  - SL/SL-M and priceless LIMIT refused;
  - LTP batching.

**Existing tests updated to PK-20 semantics.** Each asserted the old instant fill; no live assertion was weakened.

| Test | Was | Now |
|---|---|---|
| `test_live_safety::test_p0_3_dry_run_position_exit_under_global_live_sends_nothing` (×2) | executed, no order id, "Simulated", journaled at once | PAPER order, `acknowledged`, EXITING; journaled after the paper fill; still zero Kite calls |
| `test_partial_fills::test_dry_run_entry_is_filled_in_full_at_once` | filled 5 at approval | renamed `…fills_only_through_the_paper_broker`: 0 at approval, 5 after the paper fill |
| `test_account_safety::test_12h_dry_run_entry_outside_hours_is_simulated_without_amo` | dry run ignored PK-12H | renamed `…follows_the_same_amo_rule`: 409 without consent, paper AMO with it |
| `test_account_safety::test_12d_dry_run_entry_makes_no_price_check` | zero LTP calls | still no price check / refusal; exactly one LTP read (the paper GTT's reference price) |
| `test_app_positions_api::test_dry_run_position_can_be_exited_and_closes_on_approval` | CLOSED on approval | renamed `…closes_on_the_paper_fill`: EXITING, then CLOSED after the paper fill |
| `test_app_positions_api::test_query_count_does_not_grow_with_positions` | ≤ 4 | ≤ 5 (the one paper-order query); `many == one` unchanged |
| `test_review_changes::test_dry_run_is_marked_filled` | filled at 3000 (the ticket price) | renamed: pending at approval, filled at the observed 2995 |
| `tests/conftest.py` | — | resets the paper broker's test hooks per test |

# Mutation Results

Each mutation was applied, the PK-20 suite run, the file restored byte-for-byte and its sha256 re-checked. All five target files matched their pre-mutation hashes afterwards.

| # | Mutation | File | Result | Catching tests (first ones) |
|---|---|---|---|---|
| M1 | paper MARKET fills immediately at the requested price (the old `_simulate`) | `execution.py` | **caught**, 30 failing | test_1, test_2_3, no-price-stays-open, test_4 slippage, test_5_6 … |
| M2 | paper LIMIT ignores the price condition | `paper_broker.py` | **caught**, 6 | test_5_6, test_7_8, LIMIT exit, gap-through, test_22, test_26 |
| M3 | a GTT trigger closes the position without a fill (its order fills on trigger) | `paper_broker.py` | **caught**, 5 | test_14_16_17, test_15, gap-through, test_18, test_19 |
| M4 | a partial fill treated as full (fill plan ignored) | `paper_broker.py` | **caught**, 6 | test_9_10, test_11_12, test_18, test_21 partial, fill-plan restart, test_27_31 |
| M5 | dry run routed to Kite's `place_order` (broker = kite, guard off) | `routes/tickets.py` | **caught**, 31 | test_1, test_2_3, … test_25 |
| M6 | a restart creates a duplicate paper order (startup re-inserts working orders) | `db.py` | **caught**, 2 | test_22, test_23 |
| M7 | a paper exit marked CLOSED on submission (the removed confirm-time close restored) | `routes/tickets.py` | **caught**, 6 | test_4 journal, LIMIT exit, test_11_12, test_23, test_27_31, legacy exit |
| M8 | the paper GTT keeps the requested, not the open, quantity (PK-13 resize skipped) | `position_quantity.py` | **caught**, 3 | test_9_10, test_11_12, test_21 partial |

**8/8 caught.** `sha256sum -c` confirmed all five mutated files byte-identical afterwards.

# Targeted Repeats

| Run | Result |
|---|---|
| PK-20 suite (`tests/test_paper_broker.py`, 42 tests) ×10 | **10/10 green** |
| partial / restart / lifecycle subset (10 tests: `-k "partial or restart or lifecycle or fill_plan or 27_31 or 9_10 or 11_12"`) ×20 | **20/20 green** |

No flake was seen. The suite drives prices and the clock deterministically (the FakeKite LTP map, a pinned market clock and an injectable paper clock), with no sleeps.

# Browser Validation

- **Setup:** `tests/demo_server.py --paper-controls --market-open`, driven by Playwright with Microsoft Edge headless.
  - **CSP enforced** (no bypass), with a `securitypolicyviolation` listener installed before any page script.
  - `--paper-controls` registers **demo-only** routes (they do not exist in `backend/main.py`): `POST /api/demo/paper/ltp`, `/cycle` and `/fill-plan`. With them, the paper exchange advances only on demand, so every state can be observed.
  - `--market-open` pins the server's clock.
- Entries are approved **through the UI dialog**; exits and paper cancels are clicked in the Positions view.

**Result: 29/29.**

1. The approval note reads "DRY RUN — approving sends a PAPER order to the simulated broker; nothing is sent to Kite…".
2. **ENTRY_OPEN** after approval, with the DRY RUN chip, a "PAPER entry … · OPEN" line and a Cancel paper order button.
3. An LTP above the BUY limit leaves it ENTRY_OPEN.
4. At 1588 it is **OPEN**, **PROTECTED**, with a tooltip naming `PAPER-G-…` and a paper order line COMPLETE.
5. Exit, then approve: **EXITING**, with the active exit WORKING and a "PAPER exit …" line.
6. The next cycle: gone from Active, **CLOSED** in History, journaled at 1588.80 → 1619.15 (5 bps).
7. **Partial case:** 40 with plan 10/25/40 gives ENTRY_PARTIAL "10 / 40 filled" and "PARTIAL 10/40", then "25 / 40". Protection covers 25. The paper-cancel confirm modal appears; the request alone changes nothing; after the monitor the position is OPEN with 25 (entry partial).
8. The detail dialog shows "Paper Order ID" / "Paper GTT ID".

**Zero CSP violations, zero console or page errors, zero failed requests, zero 4xx/5xx.** Screenshots are in the session scratchpad (`browser/01-…07-*.png`).

**Final re-run.** The script was run again on a fresh demo after the last frontend change (the approval toast now reads "PAPER order PAPER-O-1 placed at the simulated broker — no real order was sent…"): **29/29** again, with zero CSP violations, errors or failed requests.

**Regression.** `tests/e2e_ui.py` against the default demo gave **53/54**. It is identical to the PK-19 baseline, with the same pre-existing failure ("risk-blocked signal listed separately").

# Full Suite ×3

Three consecutive runs of the whole suite, after the last change of any kind:

| Run | Result |
|---|---|
| 1 | **1,110 passed**, 0 failed (288.7 s) |
| 2 | **1,110 passed**, 0 failed (516.2 s) |
| 3 | **1,110 passed**, 0 failed (647.0 s) |

- 1,110 = the 1,068 baseline + the 42 new PK-20 tests; 0 xfailed, 0 xpassed.
- Runs 2 and 3 were slower only because the browser run and a demo server shared the machine.
- An intermediate run, before the fixes below, had 3 failures. Each was an old-semantics assertion, or a gap it revealed:
  - the PK-19 query-count ceiling (now 5);
  - a dry-run row carrying a historical Kite GTT id, which the watcher must ignore. This is now enforced by requiring `PAPER-` ids in the dry-run watcher and monitor queries;
  - `test_dry_run_is_marked_filled`, rewritten.

# Known Limitations

1. **Liquidity.** The LTP alone "proves" a fill of any size: normal runtime fills the whole remaining quantity at the first eligible price. Partials come only from a fill plan (test/demo). A volume / order-book model is later work.
2. **Paper P&L is gross.** The journal has no brokerage/STT/charges model for live or paper; costs are PK-21's. Slippage (5 bps) is the only trading cost simulated.
3. **Holiday calendar.** Sessions and AMO next-session use weekends only (PK-22).
4. **Price source.** A batched REST `ltp()` per paper cycle. The WebSocket ticker has no LTP cache and only subscribes watchlist/holdings tokens, so it was not used.
5. **Other simulation gaps:**
   - The fill price for a MARKET order is the LTP at the evaluation, not a bid/ask.
   - GTT triggers use the LTP at a cycle (20 s cadence), not tick by tick.
   - A gap between cycles is seen only at the next cycle's price.
6. **Monitoring coverage.** Paper positions are not covered by Emergency Exit, strategy exit scans, breakeven proposals, or the PK-5/PK-16 broker reconciliation. All of these were live-only before PK-20 and remain so.
7. **Wording.** Some alert texts shared with live (e.g. "check it in Kite", "place a stop-loss in Kite") also appear for paper positions, because the code paths are deliberately the same.
8. **LTP column.** The Positions view's LTP / unrealised P&L column uses the page's own LTP source (holdings/positions), not the paper exchange's observed price (PK-19 limitation 4).

# Deferred Work

- **PK-21:** historical fills, costs/brokerage, gap-aware stops, historical portfolio constraints. Not started.
- **PK-22:** holiday calendar; the `idx_dashboard_panels_account_type` re-creation noted above; `schema_version`.
- Paper coverage for Emergency Exit (a paper drill), strategy exit scans and breakeven, if wanted.
- An optional read-only "Paper orders / Paper GTTs" listing page.
- A volume-aware liquidity model.

# Deviations From Audit

1. **Audit:** "FakeKite fed by live LTP" in `kite_client.py`.
   - **Done instead:** a separate runtime `backend/paper_broker.py` with **persistent** DB books. FakeKite is in-memory test infrastructure: it would lose state on restart, and it lives in `tests/`.
   - FakeKite is untouched and remains the unit-test broker; several of its ideas are reused (Kite-shaped dicts, the GTT result link, the accept-then-raise hook).
2. **Paper ids stored in the existing `kite_order_id` / `kite_gtt_id` columns** (user decision), prefixed `PAPER-O-` / `PAPER-G-` and scoped by the ticket's mode. This keeps one lifecycle rather than forking every reader. Broker state lives in separate `paper_orders` / `paper_gtts` tables, as the packet prefers.
3. **PK-12H now applies to dry run.** A dry-run entry outside market hours needs explicit AMO consent, as the packet requires; it was previously simulated without consent. Live-only controls stay live-only.
4. **SL / SL-M orders are refused rather than simulated.** The terminal never places them, per the packet's "do not overbuild".
5. **One lifecycle query added** (PK-19's bound 4 → 5), to show the paper order's real status.
