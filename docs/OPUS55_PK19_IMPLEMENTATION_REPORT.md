# OPUS55 PK-19 Implementation Report: server-derived position lifecycle and Positions view

| | |
|---|---|
| Packet | **PK-19** only (audit *UI/UX Assessment* problem 1 and 5, plan row PK-19). PK-20 and later were not started. |
| Baseline before | **986 passed**, 0 failed, 0 xfailed, 0 xpassed, re-measured on the untouched code (`01269a7`) before the first edit. `tests/e2e_ui.py`: **53/54**, the one failure being "risk-blocked signal listed separately". |
| Result after | **1068 passed, 0 failed, 0 xfailed, 0 xpassed** (see *Full Suite ×3*). 1068 = 986 + 80 new tests (`test_lifecycle_state.py` 34, `test_app_positions_api.py` 24, `test_lifecycle_ui.py` 22) + 2 cases the PK-5 CSRF inventory generated automatically for the new POST route. No existing test was restated or removed. |
| Backups taken first | Tag `backup-pre-pk19` and branch `backup/pre-pk19`, both at `01269a7` (the tree was clean). `backend/kite_terminal.db.pre_pk19_bak` was taken with the SQLite online-backup API from a read-only connection: `integrity_check` ok, 18 tickets, 50,556 OHLC rows. |
| Live DB | **Not touched.** SHA-256 `1378f8b47cd7…dacf` before the backup, after it, and after all validation. PK-19 has **no schema change and no migration**. Tests use a per-test temporary DB and FakeKite; nothing reached Zerodha. |
| User decision | The Positions view's **Exit** action uses a **new endpoint that creates** (never confirms) a linked exit ticket, which is then approved through the existing dialog and confirm path. |

# Executive Summary

Before PK-19 the frontend worked out a trade's state from raw columns in nine places: `statusBadgeHtml`, `quantityCellHtml`, the row-flag rule, the "unprotected" banner, `hasTrailingTickets`, the Signal Detail "already hold" check, the Emergency Exit preview, `isSubmissionUnresolved` and `reportExecution`. Each read some combination of `status`, `entry_fill_status`, `submission_state`, `exit_state`, `filled_quantity`, `closed_at`, `kite_gtt_id` and `exit_in_flight_ticket_id`. Positions were only visible as tickets: an EXITING, partially filled or closed position all read "executed".

PK-19 makes the **server authoritative**:

- **`backend/lifecycle.py` (new)** is the single place that decides what state a position is in. It returns one `lifecycle_state` from 14 values, plus flags: `protection_status`, `review_required`, `entry_partial`, `attention_reasons` and `severity`. It also returns the PK-13 quantities, the active exit, the attached reconciliation findings and `allowed_actions`. A separate `ticket_state` describes the ticket and its order, so "Ticket: executed" and "Position: EXITING" are shown as two different things.
- **The derivation is pure and batched.** Everything comes from persisted, app-known state in **three queries** whatever the number of positions (every ticket of the account, the unresolved findings, the journal). No broker call is made to render anything.
- **`GET /api/app-positions?view=active|closed`** serves UI-ready positions, and **`POST /api/app-positions/{id}/exit`** creates one exit ticket for the open quantity. It is refused (409) where the lifecycle does not allow an exit, and the confirm path still enforces PK-1/3/4/8/12/13/17.
- **A new Positions view** shows Active (default) and Closed/History. It answers what is held, waiting to fill, exiting, partially filled, unprotected, needs attention or closed, with badges that carry text and an icon as well as colour.
- **`frontend/lifecycle.js` (new)** holds pure renderers, and **`app.js` no longer reads any of the raw lifecycle columns**. A static test enforces this, and Node renders real API payloads through the real frontend code.

**No execution, exit, risk or reconciliation semantics changed.** The only backend write-path change is additive: six places that log a reconciliation finding now also record a structured `issue_code`, which changes nothing about de-duplication or review.

**Validation:**

- 80 new tests;
- 13/13 focused mutations caught, with every file restored and hash-verified;
- the lifecycle suite ×10 and the partial/UNKNOWN/EXITING subset ×20, all green;
- a browser run under an enforced CSP: 27/27;
- `e2e_ui.py` identical to the baseline (53/54, same failure);
- the full suite ×3: 1068 passed each run.

# Previous UI/Lifecycle Problem

Traced in the pre-PK-19 code:

| Where (app.js) | What it decided from raw fields | Consequence |
|---|---|---|
| `statusBadgeHtml` | 10 branches over `status`, `submission_state`, `exit_state`, `entry_fill_status`, `filled_quantity`, `closes_ticket_id` | A closed position, an EXITING position's entry and a partial-then-cancelled position all read "executed". An unconfirmed entry read "executed". |
| `quantityCellHtml` | `status`, `filled_quantity`, `open_quantity`, `closes_ticket_id`, `closed_at`, `exit_state` | A second copy of the PK-13 display rules. |
| row flag (`renderTicketsTable`) | `status`, `mode`, `kite_gtt_id`, `closed_at`, `use_gtt`, `exit_state`, `filled_quantity` | A second copy of "unprotected" and "partial". |
| `buildAlerts` "unprotected" | the same five columns | Missed a residual left unprotected after a GTT partial (PK-13), because it looked only at the entry's own GTT id. |
| `hasTrailingTickets`, Signal Detail "already hold" | `status === "executed" && !closed_at` | Counted a zero-fill working entry as held. |
| Emergency Exit preview | `entry_fill_status === "pending"` | — |
| `isSubmissionUnresolved`, `reportExecution` | `status`, `submission_state`, `exit_state` | — |

There was no view of *positions*. The Trade Queue "Executed" tab listed entry and exit tickets together, and the audit's primary trader question (what do I hold, is it protected, what is working) needed three screens.

# Server-Derived Lifecycle Model

`backend/lifecycle.py`:

| Function | Role |
|---|---|
| `build_context(conn, account_id)` | 3 queries: all of the account's tickets (grouped into exits per position), unresolved `reconciliation_log` rows (by ticket, and symbol-scoped PK-16 findings by symbol), and `closed_trades` by `buy_ticket_id`. |
| `derive(row, ctx)` | The position lifecycle of an entry ticket. Pure. |
| `for_ticket(row, ctx)` | An entry's own lifecycle, or the lifecycle of the position an exit closes. `None` for a Quick Action reduction of a holding the app never opened. |
| `ticket_state(row)` | The ticket/order view `{code, label, tone, css, hint}`. It needs only the row. |
| `positions(ctx, view)`, `summary(active)` | The Positions API's lists and header counts. |
| `exit_in_flight(row)` | The Python twin of `execution.EXIT_IN_FLIGHT_SQL`; a parity test compares them. |

The quantities are not recomputed in `lifecycle.py`. `position_quantity.py` gained row-based twins of its SQL (`exits_filled_from_rows`, `open_quantity_from_rows`, `protection_quantity_from_rows`), so the PK-13 arithmetic still lives in one module. A parity test asserts they equal the SQL figures in seven states: working, partly working, ended partial, partial exit, unknown, legacy and oversold.

**Consumers now using it:**

- `routes/tickets.py`: `list_tickets`, `get_ticket`, the confirm response and the Signal Detail chart payload;
- `routes/positions.py`;
- the Emergency Exit preview (`lifecycle_state` per row);
- `signal_chart._exit_plan`, whose "is the position open" question now comes from the lifecycle.

# Lifecycle State Definitions

These are derived from the traced PK-1..PK-18 behaviour, not from an idealised machine. Rules are applied in this order for an executed entry: closed → unknown exit → unconfirmed entry or exit → working entry → unknown quantity → in-flight exit → flat-but-not-closed → shares sold → open.

| `lifecycle_state` | Label | Derived from |
|---|---|---|
| `PENDING_REVIEW` | PENDING REVIEW | `draft` |
| `READY_TO_APPROVE` | READY TO APPROVE | `pending_confirmation` |
| `SUBMITTING` | SUBMITTING | `confirmed`, not unknown; a claim whose broker call is in progress |
| `SUBMISSION_UNKNOWN` | UNKNOWN — DO NOT RESUBMIT | entry `confirmed` + `unknown` / `needs_review` / `submitting` older than `SUBMISSION_STALE_SECONDS` (`unknown_scope: entry`); **or** an executed position whose in-flight exit is in that condition (`unknown_scope: exit`) |
| `ENTRY_OPEN` | ENTRY WORKING | `executed` + `pending`, nothing filled |
| `ENTRY_PARTIAL` | ENTRY PARTIAL | `executed` + `pending`, `filled_quantity` > 0 |
| `OPEN` | OPEN | entry ended (`filled`, `partial` or legacy), open quantity > 0, no exit in flight, nothing sold. A partial-then-cancelled entry is OPEN with `entry_partial: true` |
| `EXITING` | EXITING | an in-flight exit (`executed` + `pending`, or a fresh `confirmed` claim) that has sold nothing |
| `EXIT_PARTIAL` | EXIT PARTIAL | an in-flight exit that has sold some shares, **or** ended exits (explicit, or a synthetic GTT partial) that sold some while the rest is open. `active_exit` is non-null only while an order works |
| `CLOSED` | CLOSED | `closed_at` set (only `close_if_flat` / dry-run close write it), or a legacy close (`_CLOSED_BY_EXECUTED_EXIT`) |
| `REJECTED` | REJECTED | `rejected` / `risk_rejected`; `cancelled` + `entry_fill_status='rejected'` (the broker rejected the entry); `failed` + `submission_state='rejected'` |
| `CANCELLED` | CANCELLED | `cancelled` with nothing filled |
| `FAILED` | FAILED | `failed` (`not_found` → "not placed", a local refusal, a dry-run failure) |
| `NEEDS_REVIEW` | NEEDS REVIEW | entry or exit `unconfirmed`; open quantity NULL or < 0; open 0 with the entry ended but not closed |

`OPEN_UNPROTECTED` from the brief's starting list is **not** a state; it is `OPEN` + `protection_status: MISSING` (see *State vs Flags*). "GTT mismatch" and "possible double sell" are flags and issues as well.

`ticket_state` codes keep every label the UI showed before and add three:

| Code | Label |
|---|---|
| `EXIT_WORKING_PARTIAL` | "EXITING · 15/40 sold" |
| `EXIT_FILLED` | "exit filled" (was "executed") |
| `FILL_UNCONFIRMED` | "FILL UNCONFIRMED — NEEDS REVIEW" (was "executed") |

# State vs Flags

| Flag | Values / meaning |
|---|---|
| `protection_status` | `PROTECTED`, `MISSING`, `QUANTITY_MISMATCH`, `PRICE_MISMATCH`, `UNKNOWN`, `NOT_REQUESTED` (`use_gtt=0`), `NOT_APPLICABLE` (dry run, closed, not a position) |
| `review_required` | Server-derived boolean: UNKNOWN, NEEDS_REVIEW, any protection problem, or any attached unresolved finding |
| `attention_reasons` | Why: e.g. `SUBMISSION_UNKNOWN`, `PROTECTION_MISSING`, `GTT_QUANTITY_TOO_HIGH`, `POSSIBLE_DOUBLE_SELL` |
| `entry_partial` | The entry ended having filled less than requested |
| `severity` | `critical` (UNKNOWN, NEEDS_REVIEW, missing or mismatched protection, a critical finding), `warning` (review, or a partial / exiting state), `info` (pending, working), `ok` (OPEN, protected), `muted` (terminal) |
| `is_position`, `is_holding`, `is_closed`, `is_terminal` | Convenience predicates, so the frontend never needs a list of states |

Example: an oversized GTT on an open position is `lifecycle_state: OPEN`, `protection_status: QUANTITY_MISMATCH`, `review_required: true`, `severity: critical`. It is not `OPEN_GTT_MISMATCH_REVIEW`.

# Quantity Semantics

Every lifecycle carries these fields, and the frontend never recomputes them:

- `requested_quantity`;
- `filled_quantity` (PK-13 `filled()`);
- `open_quantity` (0 once closed);
- `exited_quantity` (what executed exits sold);
- `protection_quantity`;
- for an order in flight: `exit_requested_quantity`, `exit_filled_quantity`, `remaining_quantity`.

The display rules live in `lifecycle.js`:

| Situation | Shown |
|---|---|
| normal | `40` |
| working entry | `40 / 100 filled` · "order still working" |
| ended partial entry | `40`, with `40 / 100 filled` under it |
| partial exit | `25 remaining`, with `15 exited` under it |
| unknown | `unknown` / "fill unknown" |

# Protection Semantics

Protection is derived only for **live** positions:

- `NOT_REQUESTED` if the ticket asked for no GTT.
- `UNKNOWN` if the quantity or the submission is unknown.
- `MISSING` if `kite_gtt_id` is NULL while shares must be protected. That covers `protection_failed`, a GTT the watcher found gone, the unprotected residual after a GTT partial, and an adopted entry with no GTT. It is also `MISSING` if an unresolved `GTT_MISSING` / `PROTECTION_MISSING` finding exists.
- `QUANTITY_MISMATCH` for an unresolved `GTT_QUANTITY_TOO_HIGH` / `TOO_LOW` (PK-16) or `PROTECTION_QUANTITY_MISMATCH` (PK-13 resize failure).
- `PRICE_MISMATCH` for `GTT_PRICE_MISMATCH`.
- Otherwise `PROTECTED`.

**"Last known."** Findings are never auto-resolved (PK-16), so a mismatch the GTT watcher has since healed still shows until the finding is marked reviewed. Rendering makes no broker call to re-verify a GTT (see *Known Limitations*).

# UNKNOWN Handling

`SUBMISSION_UNKNOWN` is first-class for entries **and** exits. It is never folded into PENDING, FAILED or "executed".

- The label is **"UNKNOWN — DO NOT RESUBMIT"**, with "(exit)" appended for an exit's unknown outcome.
- It is critical severity with a ⛔ icon and `review_required`.
- `protection_status` is `UNKNOWN`.
- **`allowed_actions` is only `VIEW_DETAIL`**: no Exit, no Emergency Exit (which would return `exit_outcome_unknown` anyway) and no Cancel.
- The active-exit cell reads "Exit #N · UNKNOWN — DO NOT RESUBMIT".
- A claim still `submitting` past `SUBMISSION_STALE_SECONDS` (the process died mid-call) is shown as UNKNOWN, matching `order_monitor.unresolved_submissions`.

# EXITING Handling

PK-8 is kept: **an executed exit ticket does not mean the position is closed.**

- **Position.** `EXITING` while the exit order is at the broker and has sold nothing. The exit ticket's own `ticket_state` is "EXITING · awaiting fill", and its `lifecycle` is the position's EXITING.
- **Not closed yet.** `CLOSED` appears only once `closed_at` is set by `close_if_flat` on a broker-proven full fill (or the dry-run simulation). Test 12 and the browser run check both sides.
- **While EXITING:** there is no second Exit and no Emergency Exit (it would answer `already_exiting`). `CANCEL_ORDER` is offered, linking to Orders & Trades, where the PK-9 cancel lives.
- **A rejected exit** returns the position to `OPEN` with the exit allowed again, flagged `review_required` by the finding the monitor logs (test 14).

# Partial Fill Handling

- **Entry working with fills** → `ENTRY_PARTIAL`. No Exit is offered, because the entry can still fill; the Orders cancel is offered, and Emergency Exit covers it (it cancels the rest, then sells the fill, per PK-13 decision (c)).
- **Entry ended partial** → `OPEN` + `entry_partial`, showing 40 of 100.
- **Exit working with fills** → `EXIT_PARTIAL` with an `active_exit`: no new Exit.
- **Exit ended partial** → `EXIT_PARTIAL` with no `active_exit`: **Exit allowed for the residual**, per the PK-13 ownership rule. Test 11b checks this.
- **GTT partial** (a synthetic partial exit) → `EXIT_PARTIAL`. Once the GTT order ends, `kite_gtt_id` is NULL, so the state becomes `MISSING` (test 15).

# Positions API

`backend/routes/positions.py` (new; registered in `main.py`). `/api/positions` remains the broker passthrough, so the app positions live under a distinct prefix.

- **`GET /api/app-positions?view=active|closed&limit=`** returns `{"data": [lifecycle…], "view", "summary"}`. The summary (active view only) counts open, waiting to fill, exiting, needs attention, unprotected and total.
  - **Active:** every entry that is, or may be, at the broker: `SUBMITTING`, `SUBMISSION_UNKNOWN`, `ENTRY_*`, `OPEN`, `EXIT*` and `NEEDS_REVIEW`. It is sorted by severity, then symbol.
  - **Closed:** `CLOSED` positions, newest `closed_at` first, each with `closed_trade`: journal id, the round-trip quantity, entry, **average exit**, realized P&L and %, R, exit reason and dates.
  - Tickets that never became positions (drafts, approvals, rejections) appear in neither view.
- **`POST /api/app-positions/{id}/exit`** (201):
  1. It re-derives the lifecycle.
  2. If `EXIT` is not allowed it answers 409 and creates nothing: **`EXIT_ALREADY_PENDING`** (with the pending ticket's id) when an exit already awaits review, otherwise **`EXIT_NOT_ALLOWED`** (with `lifecycle_state` and a reason).
  3. It runs `risk_engine.linked_exit_problem` against the prospective exit, the same check confirmation runs.
  4. It creates a MARKET SELL for the open quantity in the position's own mode, through `kill_switch.create_linked_exit_ticket`. That builder was extracted from `_create_emergency_exit_ticket`, which now delegates to it with its old rationale and checklist, byte-identical as a test checks.
  5. It runs the risk checks and returns the ticket, normally `pending_confirmation`.

  Nothing is confirmed or sent. The reference price is one `kite.ltp()` for that symbol at click time, the same source Emergency Exit uses. The route is a POST, so the PK-5 Origin/header guard applies automatically, and the PK-5 inventory test picked it up.
- **Ticket payloads** (`GET /api/tickets`, `GET /api/tickets/{id}`, confirm, chart) carry `ticket_state` and `lifecycle`. `exit_state` and `exit_in_flight_ticket_id` are kept for compatibility; the frontend no longer reads them.

**App positions vs broker holdings.** Only tickets this terminal opened are positions. Holdings, family or external holdings, and broker positions stay on the Dashboard.

# Active Positions View

- **Navigation.** A new **Positions** nav item, first under *Trading*, with a badge showing the needs-attention count. It is `#view-positions`, with **Active** as the default tab and then **Closed / History**.
- **Header.** Summary chips: open · waiting to fill · exiting · need attention · unprotected.
- **Columns.** Symbol (strategy) · Lifecycle · Mode · Qty · Entry (fill, or "ref.") · LTP · Stop · Target · Protection · Active exit · Unrealised P&L · Review (NEEDS REVIEW plus issue badges) · Actions.
- **LTP** comes from what the page already keeps live (`state.holdings` / `state.positions.net`, updated by `/ws/ticks`), with no extra fetch. On a tick only the LTP and P&L cells are updated, so rows and buttons are not rebuilt under the cursor.
- **Unrealised P&L** is shown only for OPEN, EXITING or EXIT_PARTIAL with a known entry fill and open quantity > 0; otherwise "—".
- **Risky rows** get a left bar by severity. Every badge has its text label and an icon (⛔ ⚠ 🛡 ◷ ●), so colour is never the only cue.
- **Refresh.** The view reloads on section show, on every `tickets_changed` broadcast (via `refreshTicketViews`) and from its Refresh button.

# Closed / History View

Columns: Symbol · Strategy · Lifecycle (CLOSED) · Mode · Qty (the journal's round-trip quantity) · Entry · Avg exit · Opened · Closed · Realised P&L · Review · Actions (Journal, Details).

- The figures come from `closed_trades`, using the PK-13 journal semantics: one row per round trip at the weighted exit price.
- No partial realised P&L is invented before closure.
- A closed position with an unresolved finding (for example `GTT_ON_CLOSED_POSITION`, `ORPHANED_GTT_AFTER_EXIT` or `POSSIBLE_DOUBLE_SELL`) still shows NEEDS REVIEW and its issue badges.

# Allowed Actions

UX guidance only; the server re-checks on invocation.

| Action | Offered when | Button |
|---|---|---|
| `EXIT` | `OPEN`, or `EXIT_PARTIAL` with no exit in flight; open quantity > 0 and known; no pending exit ticket; live or dry run | **Exit**: creates the ticket, then opens the approval dialog |
| `REVIEW_EXIT` | an exit ticket in `draft` / `pending_confirmation` / `risk_rejected` already exists | **Review exit #N** |
| `EMERGENCY_EXIT` | live, no exit in flight, `ENTRY_*` / `OPEN` / `EXIT_PARTIAL` (what Emergency Exit would act on) | the header button; not per row |
| `CANCEL_ORDER` | live `ENTRY_OPEN` / `ENTRY_PARTIAL`, or an exit order WORKING / PARTIAL | **Cancel order…** → Orders & Trades |
| `VIEW_RECONCILIATION` | issues attached | **Reconciliation** → the reconciliation panel |
| `VIEW_JOURNAL` | closed with a journal row | **Journal** |
| `VIEW_DETAIL` | always | **Details** |

**Examples.** OPEN: Exit + Emergency Exit. EXITING: Cancel order only. SUBMISSION_UNKNOWN: Details only. CLOSED: Journal + Details. OPEN unprotected: Exit, Reconciliation. Ended partial exit: Exit for the residual.

**Dry-run positions** can be exited through this path: the exit inherits `dry_run` and closes on approval through the simulated fill. That is a real gap closed, since before PK-19 a dry-run position had no exit path (P1-13). They are never covered by Emergency Exit.

# Signal/Ticket UI Changes

- **`statusBadgeHtml`** renders the server's `ticket_state`. The ten branches are gone.
- **Ticket rows** show a **position lifecycle chip** next to the ticket state, replacing the old "exiting" chip. An executed exit row therefore reads "EXITING · awaiting fill" with a position chip of EXITING, and a closed entry's chip reads CLOSED.
- **The row flag, the unprotected banner, the trailing indicator, the Signal Detail "already hold" warning, the Emergency Exit preview, `isSubmissionUnresolved` and `reportExecution`** all read `lifecycle` / `ticket_state`. The banner text is now "no active protective stop — UNPROTECTED position (N share(s))", because it covers every missing-protection case.
- **The ticket detail dialog** shows **Ticket:** `<ticket_state>` and **Position:** `<lifecycle>` (#id when it is another ticket's position). When relevant it adds Held quantity, Protection, Exit and Review rows.
- **The Signal Detail header** shows "Ticket …" and, for a position, "Position … Protection …".
- **Mode.** Always the position's own `mode` (PK-3). The global switch is never used to label a position (test 18, mutant M4).
- **Static enforcement.** `test_app_js_no_longer_derives_lifecycle_from_raw_columns` asserts that `entry_fill_status`, `exit_state`, `submission_state`, `closed_at` and `exit_in_flight_ticket_id` do not appear in `app.js` code (comments excluded).

# Reconciliation Integration

- **Attached per position** as `{id, code, label, severity, detail, suggested_action, expected_quantity, broker_quantity}`. The label comes from a server-side code → label map; the frontend shows the detail as a tooltip and never parses it.
- **Symbol-scoped PK-16 findings** (`BROKER_POSITION_SHORTFALL`, `UNLINKED_POSSIBLE_GTT`, `MULTIPLE_ACTIVE_GTTS`, `AMBIGUOUS_GTT_OWNERSHIP`, all recorded with no ticket) attach to every open position of that symbol.
- **Critical findings:** `POSSIBLE_DOUBLE_SELL`, `ORPHANED_GTT_AFTER_EXIT`, `GTT_ON_CLOSED_POSITION`, `ORPHAN_GTT`, `MULTIPLE_ACTIVE_GTTS`, `BROKER_POSITION_SHORTFALL`.
- **Issue codes added at their write sites.** These are log-only keyword arguments on existing `log_mismatch` / `position_quantity.flag` calls. `flag` gained `**structured`. Non-suppressed codes still de-duplicate on (ticket, check_type, actual), so nothing else changes.

  | Code (in `reconciliation.py`) | Written by |
  |---|---|
  | `PROTECTION_MISSING` | `sync_protection` (no GTT, or GTT not active); `order_monitor` adopted entry filled without a GTT |
  | `PROTECTION_QUANTITY_MISMATCH` | `sync_protection` (resize failed, book unreadable) |
  | `POSITION_QUANTITY_REVIEW` | `sync_protection` / `close_if_flat` (open quantity unknown or negative) |
  | `POSSIBLE_DOUBLE_SELL` | `remove_protective_gtt` (GTT triggered alongside a filled exit) |
  | `ORPHANED_GTT_AFTER_EXIT` | `remove_protective_gtt` (delete failed after the close) |

- **Other uncoded findings** show as a generic "Reconciliation finding", with the detail in the tooltip. Examples are the PARTIAL EXIT flag, a rejected-exit note, and findings written before PK-16.
- **The PK-16 workflow is unchanged:** the reconciliation panel, Mark Reviewed, the review suppression and the two-cycle confirmation. A reviewed finding is no longer attached (test 16).

# Performance / Query Behavior

| Request | Before | After |
|---|---|---|
| `GET /api/app-positions` (either view) | — | **3 SELECTs**, constant |
| `GET /api/tickets` | 1 list query + 1 in-flight query + **1 open-quantity query per executed entry** (N+1) | 1 list query + **3** context queries, constant |
| `GET /api/tickets/{id}` | 3 | 5 (ticket, events, 3 context) |
| broker calls to render any of these | 0 | **0** |

- `test_query_count_does_not_grow_with_positions` counts SELECTs through a traced connection for 1 vs 9 positions (each with an exit, plus closed ones) on all three list endpoints: equal, and ≤ 4.
- `test_reading_positions_makes_no_broker_call` asserts FakeKite recorded nothing.
- The context reads every ticket of the account once. With the single-user, one-trading-account design this is hundreds of rows. A time-window filter can be added if the table ever grows large.

# Tests Added

**`tests/test_lifecycle_state.py`: 34 tests.** They are driven through create → confirm → `order_monitor` / GTT watcher / reconciliation cycles on FakeKite, with direct seeding only for edge states. There are no sleeps.

| Brief # | Test |
|---|---|
| 1, 2 | `test_1_draft_entry_is_pending_review`, `test_2_pending_confirmation_is_ready_to_approve` |
| 3 | `test_3_submitted_live_entry_is_entry_open` |
| 4 | `test_4_unknown_entry_is_submission_unknown_never_generic` |
| 5 | `test_5_zero_fill_cancelled_entry_is_cancelled_and_not_a_position`, `test_5b_broker_rejected_entry_is_rejected` |
| 6 | `test_6_partial_fill_then_cancel_is_an_open_40_share_position`, `test_6b_entry_still_working_…_is_entry_partial` |
| 7, 8 | `test_7_8_fully_filled_protected_position_is_open` |
| 9 | `test_9_open_position_without_its_gtt_is_unprotected`, `test_9b_…_not_requested` |
| 10 | `test_10_exit_submitted_is_exiting_not_closed` |
| 11 | `test_11_exit_partly_filled_is_exit_partial`, `test_11b_exit_ended_after_a_partial_fill_leaves_the_rest_exitable` |
| 12 | `test_12_exit_complete_is_closed_only_after_the_full_fill` |
| 13 | `test_13_unknown_exit_is_submission_unknown_on_the_position` |
| 14 | `test_14_rejected_exit_leaves_the_position_open` |
| 15 | `test_15_partial_gtt_fill_leaves_an_open_residual` |
| 16 | `test_16_reconciliation_issue_sets_review_required` (and a reviewed finding detaches) |
| 17 | `test_17_closed_position_never_reports_open` (`closed_at` and legacy close) |
| 18 | `test_18_mode_is_the_positions_own_never_the_global_switch` |
| 19 | `test_19_open_quantity_is_pk13s` |
| 20 | `test_20_gtt_quantity_and_price_mismatch_are_reflected` |
| extra | unconfirmed fill → NEEDS_REVIEW; unknown open quantity; `not_found` → FAILED / not placed; a pending exit gives REVIEW_EXIT not EXIT; an untracked reduction has no lifecycle; list == detail lifecycle; **row/SQL quantity parity and in-flight predicate parity**; issue codes on the five write sites (×3 tests); serializing writes nothing |

**`tests/test_app_positions_api.py`: 24 tests.**

- Active excludes closed and history includes closed; drafts and rejections are in neither.
- Requested / filled / open / exited quantities and the active exit.
- Partial-entry quantities.
- Allowed actions for OPEN / EXITING / CLOSED (×3).
- Summary counts.
- Dry run clearly identified.
- No broker call on read.
- Constant query count on 3 endpoints (×3).
- Structural reconciliation issues.
- **Exit endpoint:**
  - creates one pending ticket and sends nothing, and a second click gets `EXIT_ALREADY_PENDING`;
  - live approval → EXITING → the monitor's fill → CLOSED plus a journal row;
  - a dry-run exit closes on approval with no broker order;
  - refused for EXITING / UNKNOWN exit / CLOSED / working entry / pending exit (×5), with no ticket and no order created;
  - 404 for an unknown id or an exit ticket;
  - a CSRF-less cross-origin POST is refused;
  - Emergency Exit is unchanged by the shared builder, and the preview carries `lifecycle_state`.

**`tests/test_lifecycle_ui.py`: 22 tests.** Real API payloads are rendered in Node (`tests/js/render_lifecycle.js`) through `frontend/lifecycle.js` **and the ticket-rendering functions extracted verbatim from `frontend/app.js`**.

- OPEN badge and Exit button.
- EXITING never says CLOSED; the exit ticket's badge is EXITING, not "executed".
- The UNKNOWN warning (DO NOT RESUBMIT, critical, Details only).
- Partial entry quantity ("40 / 100 filled", "Req 100 · Filled 40 · Open 40").
- Partial exit quantity ("25 remaining", "15 exited", "Exit 40 · Filled 15 · Remaining 25").
- Unprotected shown by text and icon.
- A closed history row (average exit, +₹6,000.00).
- Buttons exactly follow `allowed_actions`.
- The mode displayed from the position.
- The reconciliation issue is visible.
- The P&L uses the open quantity and the fill price.
- Static checks: the load order in `index.html`; no raw lifecycle columns in `app.js` (×5) or `lifecycle.js` (×4); `statusBadgeHtml` renders `ticket_state`.
- The file skips with a reason if Node.js is absent. Node v24 is installed here, and nothing was skipped.

# Mutation Results

`pk19_mutations.py` (scratchpad, not committed) ran once on the final code. For each mutant it applied one exact-anchor edit (each anchor unique), ran the three PK-19 files, restored the original bytes in `finally` and verified the SHA-256.

| # | Mutant | File | Result (failing tests) |
|---|---|---|---|
| M1 | executed exit treated as CLOSED immediately | `lifecycle.py` | **caught**, 15 (tests 10, 11, 11b, 12, 15, 19, the API EXITING/summary/live-exit tests, the UI EXITING/partial-exit/P&L tests) |
| M2 | partial filled quantity ignored (open = requested) | `position_quantity.py` | **caught**, 8 (tests 3, 6, 6b, 19, unknown quantity, parity, API partial entry, UI partial entry) |
| M3 | UNKNOWN submission ignored | `lifecycle.py` | **caught**, 3 (tests 4, 13, UI unknown warning) |
| M4 | global mode instead of the position's mode | `lifecycle.py` | **caught**, 3 (test 18, API dry run, UI mode) |
| M5 | PROTECTED although the GTT is missing | `lifecycle.py` | **caught**, 4 (tests 9, 15, API summary, UI unprotected) |
| M5b | PROTECTED although the GTT quantity mismatches | `lifecycle.py` | **caught**, 3 (test 20, API issues, UI issue) |
| M6 | EXIT allowed while EXITING | `lifecycle.py` | **caught**, 5 (tests 10, 11, API actions, API refusal, UI EXITING) |
| M7 | frontend badge reverts to `ticket.status` | `app.js` | **caught**, 2 (UI EXITING, UI unknown) |
| M7b | lifecycle badge shows "executed", not the lifecycle | `lifecycle.js` | **caught**, 1 (UI OPEN badge) |
| X1 | per-position query (N+1) | `routes/positions.py` | **caught**, 2 (query count, active and closed) |
| X2 | closed view includes open positions | `lifecycle.py` | **caught**, 1 |
| X3 | reconciliation issues ignored for `review_required` | `lifecycle.py` | **caught**, 2 (tests 14, 16) |
| X4 | exit endpoint skips the allowed-action check | `routes/positions.py` | **caught**, 6 (creation + all 5 refusal cases) |

**13/13 caught, no survivors.** `restored: True`. The SHA-256 values after restore:

| File | SHA-256 |
|---|---|
| `lifecycle.py` | `1c7b013070d9…` |
| `position_quantity.py` | `b461b06a6910…` |
| `routes/positions.py` | `18abaaa68b27…` |
| `app.js` | `7d2c98a1bbb8…` |
| `lifecycle.js` | `19ff28d3151d…` |

No code changed after the mutation run; only this report was written afterwards.

# Targeted Repeat Results

`pk19_repeats.py` (scratchpad) ran on the final code, after the mutation run.

| Selection | Runs | Result, every run | Time per run |
|---|---|---|---|
| **Lifecycle targeted suite:** `test_lifecycle_state`, `test_app_positions_api`, `test_lifecycle_ui`, `test_partial_fills`, `test_exit_lifecycle`, `test_live_safety`, `test_gtt_reconciliation`, `test_emergency_exit_isolation`, `test_mode_binding`, `test_baseline_api`, `test_csrf_protection` | 10 | **482 passed**, 0 failed, 0 error | 141–166 s; run 7 took 1,064 s (see below) |
| **Partial / UNKNOWN / EXITING subset:** the three PK-19 files + `test_partial_fills` + `test_exit_lifecycle`, `-k "partial or unknown or exiting or exit_"` | 20 | **81 passed**, 0 failed | 28.9–29.7 s |

- There were no flaky results. PK-19 adds no threads, locks or timers; the derivation is pure.
- Targeted run 7 took 1,064 s instead of ~145 s, yet still passed 482/482. The runs before and after it were normal, and no test hung (there was no failure and no timeout). The cause was not identified: nothing in the output shows one, so it is recorded here rather than guessed at.

# Browser Validation

**Setup.**

- `tests/demo_server.py --port 8766 --lifecycle-fixtures` (FakeKite, a throwaway DB, and the real background loops running).
- The new opt-in flag seeds, after the normal demo data, one app position per state: LT ENTRY_PARTIAL (40/100), NEWLISTCO ENTRY_OPEN, BHARTIARTL EXITING, HDFCBANK EXIT_PARTIAL (15 of 40 sold), ITC OPEN unprotected (with a `PROTECTION_MISSING` finding), RELIANCE OPEN with a `GTT_QUANTITY_TOO_HIGH` finding, DIVISLAB entry UNKNOWN, TCS exit UNKNOWN (`needs_review`), INFY dry-run OPEN, and SUNPHARMA CLOSED and journaled.
- Every live state is backed by a matching FakeKite order or GTT, so the order monitor, the GTT watcher and reconciliation leave it stable.
- Driven by Playwright with Microsoft Edge headless. **CSP enforced** (no `bypass_csp`), with a `securitypolicyviolation` listener installed before any page script.

**`pk19_browser.py`: 27/27 checks passed.**

- The page is served with CSP `script-src 'self'`, and `lifecycle.js` loads under it.
- The summary chips are shown.
- **Every active row's lifecycle badge and buttons equal the server's `lifecycle_state` / `allowed_actions`** (checked row by row against `/api/app-positions`).
- No active row says CLOSED.
- Each state, checked in the rendered row:

  | Row | Shown |
  |---|---|
  | INFY live | OPEN + Exit |
  | LT | ENTRY PARTIAL, "40 / 100 filled", no Exit |
  | NEWLISTCO | "0 / 50 filled" |
  | BHARTIARTL | EXITING + "Exit #31 · working · 0/30 sold", no Exit, not CLOSED |
  | HDFCBANK | EXIT PARTIAL, "25 remaining", "15 exited" |
  | ITC | UNPROTECTED (text + ⛔) and NEEDS REVIEW |
  | RELIANCE | GTT QTY MISMATCH + "GTT quantity too high" |
  | DIVISLAB | "DO NOT RESUBMIT", Details only |
  | TCS | "DO NOT RESUBMIT (exit)" |
  | INFY dry run | DRY RUN, from its own mode |

- Risky rows carry ⛔ / ⚠ text icons.
- Closed history: SUNPHARMA CLOSED, average exit 1,500.00, realised **+₹1,200.00**.
- The detail dialog shows Ticket and Position separately (Position: EXITING).
- Ticket History: the exit ticket reads "EXITING · awaiting fill".
- **The real Exit flow on ITC:**
  1. Exit opens the normal approval dialog (SELL 100).
  2. Creating it sent nothing: ITC is still OPEN, with REVIEW_EXIT instead of EXIT.
  3. Approving it makes ITC **EXITING, not CLOSED**.
  4. The demo's order monitor saw the fill and moved ITC into **Closed** history, and it left the active list.
- **Zero CSP violations, zero console or page errors, zero failed requests or 4xx/5xx.**

Screenshots were saved: `01-positions-active.png`, `02-positions-closed.png`, `03-detail-dialog.png` and `04-after-exit-approval.png`.

The first two attempts stopped on harness problems, not app problems:

1. A Windows console encoding error printing "⚠".
2. A click landing on a row the section-load refresh was re-rendering. The script now waits 800 ms and clicks through the DOM.

Each re-run used a fresh demo server. A final re-run after the last CSS change is recorded under *Full Suite ×3*.

# Full Suite ×3

These were three consecutive runs on the final code, after the mutation run and its restore.

| Run | passed | failed | xfailed | xpassed | skipped | time |
|---|---|---|---|---|---|---|
| 1 | **1068** | 0 | 0 | 0 | 0 | 283.8 s |
| 2 | **1068** | 0 | 0 | 0 | 0 | 284.2 s |
| 3 | **1068** | 0 | 0 | 0 | 0 | 284.2 s |

- **Count.** 1068 = 986 (baseline) + 80 (PK-19) + 2 (the PK-5 CSRF inventory parametrises every mutating route, so the new `POST /api/app-positions/{id}/exit` added its two cross-origin cases automatically).
- **Existing tests.** One existing test caught a real problem during development: PK-5's `test_frontend_mutations_send_the_application_header` requires every raw `fetch(...)` in `app.js` to be a bare GET, and a first draft of `API.appPositions` had a template-literal `fetch`. It was fixed by routing it through `fetchJson(url, wholeBody)`. No existing test was changed.
- **Offline.** No test reached the network: FakeKite throughout.
- **Live DB.** Its SHA-256 after all runs is still `1378f8b47cd752bad016b5d5eed35b4e1317f529d32e388368b980add003dacf`, identical to the value recorded before the first edit.

**Final browser re-run.** The browser script was run once more on a fresh demo server after the last change of any kind (a `white-space: nowrap` CSS rule for the quantity column): **27/27** again, with zero CSP violations, errors or failed requests.

# Pre-existing E2E Failure Comparison

| | Before (untouched `01269a7`, recorded before the first edit) | After (final code) |
|---|---|---|
| `tests/e2e_ui.py` against the default demo on port 8765 | **53/54**; FAIL "risk-blocked signal listed separately" | **53/54**; FAIL "risk-blocked signal listed separately" |

- The PASS/FAIL list of all 54 check names was diffed between the two runs: **identical**.
- PK-19 did not fix or disturb it. It concerns the Approvals inbox's risk-blocked card count on the demo data, which PK-19 does not touch.
- The `--lifecycle-fixtures` data is opt-in, so the default demo is unchanged.
- The E2E script was **not** modified.

# Known Limitations

1. **Protection is "last known".**
   - It reflects persisted linkage (`kite_gtt_id`) and unresolved findings. Rendering never calls the broker, so a GTT deleted in Kite since the last GTT-watcher or reconciliation cycle shows PROTECTED until that cycle records it.
   - A mismatch the watcher has healed shows until Mark Reviewed (PK-16 findings are never auto-resolved).
2. **A GTT whose own sell order is still working after a partial trigger** shows `EXIT_PARTIAL` with `PROTECTED` (PK-13 keeps `kite_gtt_id` while that order works) and offers Exit for the residual, exactly as the server's `linked_exit_problem` allows. The app does not track that GTT order as an "active exit" (it has no ticket of its own). This was not changed, because it is PK-13 execution semantics.
3. **Uncoded findings** (a PARTIAL EXIT note, a rejected-exit note, rows from before PK-16) are shown as a generic "Reconciliation finding" with the detail in a tooltip. Only the six codes listed above were added at their write sites.
4. **Unrealised P&L** needs an LTP the page already has, from holdings or broker positions. A symbol the broker does not show (for example a dry-run-only position) displays "—". When the market is closed it uses the last known price.
5. **Two simultaneous Exit clicks** (two tabs) could each create a pending exit ticket, because the endpoint checks and then inserts with no lock. Only one can ever execute (the PK-4 unique index and the PK-13 claim predicate); the other is refused or superseded. Within one tab the button is disabled while the request is in flight.
6. **`opened_at`** is the entry's `filled_at` as `order_monitor` records it (the monitor's clock for an entry fill, as since PK-8), otherwise `executed_at`.
7. **The lifecycle context reads every ticket of the account per request.** That is fine at this scale (single user, hundreds of tickets); a date window can be added later.
8. **`reconciliation_log` has no `account_id`** (unchanged, PK-16). Findings are matched by ticket id and symbol, which is correct for the single trading account.

# Deferred Work

Nothing below was implemented.

- **PK-20:** paper broker.
- **PK-21:** backtester realism.
- **PK-22:** hardening, including splitting `app.js` into modules and removing `style-src 'unsafe-inline'`.
- **PK-23–PK-28:** volume, smart-money, validation, scanner and derivatives work.
- **Multi-account family portfolio;** short-selling and MIS lifecycles.
- **Possible follow-ups:**
  - issue codes on the remaining uncoded finding sites (the PARTIAL EXIT note, the rejected-exit note, GTT-watcher closure findings);
  - auto-resolving a finding when its condition is observed healthy again (PK-16 design decision);
  - tracking a triggered GTT's working order as the position's active exit;
  - a per-row "cancel this order" that calls the PK-9 cancel directly, instead of linking to Orders & Trades.

# Deviations From Audit

1. **A separate `lifecycle.py` with a batch context,** not a function in `routes/tickets._serialize`. The audit suggested computing it in `_serialize`. A module shared by the routes, the kill-switch preview and `signal_chart`, fed in 3 queries, avoids scattering the rules and removed the old N+1 in `list_tickets`.
2. **No `OPEN_UNPROTECTED` / GTT-mismatch states.** Protection is a flag, as the brief preferred ("state vs flags").
3. **A new write endpoint,** `POST /api/app-positions/{id}/exit`, which the user chose. It only creates a ticket; approval and execution use the unchanged confirm path. As a side effect **dry-run positions can now be closed** (they had no exit path before, P1-13).
4. **Issue codes at write sites.** This touches `order_monitor.py` (1 call) and `position_quantity.py` (5 calls), keyword arguments only, so the lifecycle reads findings as data instead of interpreting their text. The execution, exit, risk and reconciliation behaviour is unchanged, and the de-duplication keys are unchanged.
5. **`exit_state` and `exit_in_flight_ticket_id` are kept** in ticket payloads for compatibility (existing tests use them); the frontend no longer reads them.
6. **Label changes on the ticket side:**
   - an exited exit ticket reads "exit filled", not "executed";
   - an unconfirmed entry reads "FILL UNCONFIRMED — NEEDS REVIEW", not "executed";
   - a working exit with fills reads "EXITING · 15/40 sold";
   - the unprotected banner now covers every missing-protection case, with the share count.
7. **`signal_chart._exit_plan`** asks the lifecycle whether the position is open (`is_holding`). A zero-fill working entry now gets the "not armed yet" wording, which is correct: nothing is held.
8. **`tests/demo_server.py`** gained an opt-in `--lifecycle-fixtures` flag, off by default, so browser validation can show every state without changing the default demo or the E2E baseline.
9. `docs/OPUS55_AUDIT_REPORT.md` was **not** modified.

# Files Changed

| File | Change |
|---|---|
| `backend/lifecycle.py` | **new** — states, flags, protection, allowed actions, `ticket_state`, the batch context, the positions list and summary |
| `backend/routes/positions.py` | **new** — `GET /api/app-positions`, `POST /api/app-positions/{id}/exit` |
| `backend/main.py` | registers the router |
| `backend/position_quantity.py` | row-based quantity twins; `flag(**structured)`; issue codes in `sync_protection`, `close_if_flat`, `remove_protective_gtt` |
| `backend/reconciliation.py` | five issue-code constants (no logic change) |
| `backend/order_monitor.py` | issue code on the adopted-without-GTT finding |
| `backend/routes/tickets.py` | `ticket_state` + `lifecycle` on payloads from one context; `_quantity_fields` / `_exits_in_flight` from the context (N+1 removed); lifecycle on the chart payload |
| `backend/routes/kill_switch.py` | `create_linked_exit_ticket` extracted (Emergency Exit unchanged); `lifecycle_state` in the preview |
| `backend/signal_chart.py` | `ticket_state` / `lifecycle` in the payload; `_exit_plan` uses the lifecycle |
| `frontend/lifecycle.js` | **new** — pure renderers |
| `frontend/app.js` | raw-column lifecycle logic removed; Positions view; detail and Signal Detail show Ticket vs Position |
| `frontend/index.html` | Positions nav item and view; `lifecycle.js` before `app.js`; the Signal Detail lifecycle slot |
| `frontend/style.css` | lifecycle, protection, issue and summary styles |
| `tests/demo_server.py` | opt-in `--lifecycle-fixtures` |
| `tests/test_lifecycle_state.py`, `tests/test_app_positions_api.py`, `tests/test_lifecycle_ui.py`, `tests/js/render_lifecycle.js` | **new** |
