# PK-2 — Unknown Broker Submission Outcome: Implementation Report

| | |
|---|---|
| Packet | PK-2 from `docs/OPUS55_AUDIT_REPORT.md` (fixes P0-2) |
| Base revision | `13ae3f8` on `feature/signal-detail-and-strategy-revision`, plus the uncommitted PK-0 and PK-1 work |
| Backups taken first | tag `backup-pre-pk2-unknown-outcome` and branch `backup/pre-pk2-unknown-outcome` (both at `13ae3f8`); tag `backup-pre-pk2-worktree` (commit `3485592`: a snapshot of the working tree *including* the uncommitted PK-0/PK-1 files, made through a temporary index so the real index and working tree were not touched); `backend/kite_terminal.db.pre_pk2_bak` (SQLite online-backup API; `integrity_check` = ok) |
| Committed | No |
| Audit report | Not modified |

---

## 1. Executive summary

A live broker submission can now end in one of two classes of failure, and they are no longer collapsed:

- **Definitely rejected.** Kite answered with `OrderException`, `InputException` or `PermissionException`. The ticket becomes `failed`, as before.
- **Outcome unknown.** Anything else: `NetworkException`, `TokenException`, `GeneralException`, `DataException`, raw `requests` errors (which kiteconnect re-raises unwrapped), a malformed response, or any other exception once the call has started. The order may or may not exist.

On an unknown outcome:

- the confirm returns **HTTP 200** with `success: false` and `Entry/Exit order outcome UNKNOWN — DO NOT RESUBMIT …`, instead of a 500 or a "failed" ticket;
- the ticket stays `status='confirmed'` with `submission_state='unknown'`, so it is never confirmable, editable, rejectable or deletable again, and never returns to `pending_confirmation` (PK-1's invariant is intact);
- nothing is retried;
- `order_monitor` searches Kite's order book for the ticket's **deterministic order tag**, which every entry and exit order now carries, and resolves the ticket **forward only**:
  - one matching order → **adopted** (`executed`; the normal fill lifecycle continues);
  - none within 15 minutes on the same IST day → **not_found** (`failed`; nothing was placed);
  - contradictory or uncheckable evidence → **needs_review** (urgent; nothing guessed).

All the information recovery needs is written to the database **in the claim itself**, before any broker call. A process that dies inside `place_order` is recovered after restart from the database and the broker alone.

Both writers — the confirming request and `order_monitor` — now resolve a live ticket only through compare-and-set updates. A late writer never overwrites a newer state.

Four PK-0 strict xfails are now normal passing tests, and 28 new test cases were added. The full suite ran green three times in a row: **165 passed, 13 xfailed, 0 failed, 0 xpassed**. The targeted PK-1/PK-2 subset (48 tests) passed 20 consecutive times.

## 2. Files changed

| File | Change |
|---|---|
| `backend/execution.py` | Submission-state constants and documentation; `DEFINITE_REJECTIONS`; `order_tag_for()`; `utc_now_sql()`; `update_ticket_if()` (compare-and-set); `unknown_result()`; tag passed to `place_order` for entries and exits; only `place_order` itself sits inside the `try`; the original-GTT deletion extracted to `cancel_original_gtt()` (shared with the monitor). |
| `backend/routes/tickets.py` | Live claims record `order_tag`, `submission_state='submitting'` and `submitted_at` in the PK-1 compare-and-set. Outcome writes are conditional (`_record_execution_result`). A lost outcome write is handled by `_reconcile_late_result`. An exception escaping `execute_ticket` on a live ticket becomes an unknown outcome, not a 500. Exit closure runs only for the writer that won. |
| `backend/order_monitor.py` | `unresolved_submissions()`, `_reconcile_submission()`, `_adopt()`, `_needs_review()`, `_not_found()`, `_gtt_is_active()`, a monkeypatchable `_utc_now()`. The existing resolution writes go through the conditional `_claim_pending()`. Adopted entries are checked for protection when they fill. |
| `backend/journal.py` | `close_position_for_exit()`: the confirm path's "stamp closed_at + record closed_trades" block, extracted unchanged so the monitor's exit adoption does exactly the same thing. |
| `backend/db.py` | Migrations `_ensure_submission_unknown_event_type` (ticket_events rebuild) and `_ensure_submission_tracking_columns` (3 × `ALTER TABLE`). |
| `backend/config.py` | `SUBMISSION_STALE_SECONDS = 120`, `UNKNOWN_ORDER_RECONCILE_WINDOW_SECONDS = 900`. |
| `backend/routes/kill_switch.py` | Emergency Exit All skips a position whose exit is already claimed (`status='confirmed'`), instead of sending a second SELL. |
| `frontend/app.js`, `frontend/style.css` | Badges, toast, row flag and a tag row (section 13). |
| `tests/fake_kite.py` | `place_order(…, tag=None)` records the tag and stores `tag`/`tags` on the order; `add_broker_order(…)` seeds an order that already exists at the broker without recording a `place_order` call. |
| `tests/test_live_safety.py` | Four xfail markers removed and those tests strengthened; 28 new test cases; helpers. |
| `docs/OPUS55_PK2_IMPLEMENTATION_REPORT.md` | New. This report. |

Untouched: `risk_engine.py`, `trailing_engine.py`, `reconciliation.py`, `strategy_engine.py`, the audit report.

## 3. Schema change

Two idempotent migrations, run automatically by `init_db()`:

| Migration | Change | Position in `init_db()` |
|---|---|---|
| `_ensure_submission_unknown_event_type` | Rebuilds `ticket_events` with the new event type `submission_unknown`. It uses the established pattern (`_ensure_stop_adjusted_breakeven_event_type`) with the full current list. | Immediately after `_ensure_stop_adjusted_breakeven_event_type` |
| `_ensure_submission_tracking_columns` | `ALTER TABLE trade_tickets ADD COLUMN order_tag TEXT`, `submission_state TEXT`, `submitted_at TEXT` (all nullable) | **After** `_ensure_alert_action_source`, whose rebuild copies an explicit column list and would silently drop later columns on an old database |

No `trade_tickets` rebuild. The `status` CHECK constraint is unchanged, because no new status value was needed (section 4). `reconciliation_log` reuses its existing `order_status` and `position_state` check types. No `broker_orders` table was added.

**Checked against real data.** `init_db()` was run twice against a scratch copy of `kite_terminal.db.pre_pk2_bak`; the live database was never opened.

- Row counts of `trade_tickets` (18), `ticket_events` (49), `closed_trades`, `ohlc_daily` (50,556), `instrument_cache` (10,133) and `reconciliation_log` were unchanged.
- Both migrations applied, and the second run was a no-op.
- `integrity_check` = ok; `foreign_key_check` returned no rows.
- No legacy row got a `submission_state`.

## 4. Exact new ticket / submission states

`status` keeps its existing values. The new `submission_state` column says what is known about a **live** ticket's broker order:

| `submission_state` | `status` | Meaning | Who writes it |
|---|---|---|---|
| `NULL` | any | Never submitted: draft, pending or rejected tickets, dry runs, and every pre-PK-2 row | — |
| `submitting` | `confirmed` | Execution claimed; the broker call is about to start or is in flight. Written **in the same compare-and-set** as `pending_confirmation → confirmed`. | confirm (claim) |
| `acknowledged` | `executed` | Kite returned an order id (normal path) | confirm |
| `rejected` | `failed` | Kite definitively rejected the order | confirm |
| `unknown` | `confirmed` | The outcome is unknown and the monitor is searching for it. Also reached from a `submitting` claim older than 120 s (crash). | confirm, monitor |
| `adopted` | `executed` | The tagged broker order was found and taken over; normal lifecycle from here | monitor |
| `not_found` | `failed` | No tagged order within the window on the same IST day: nothing was placed. **Final.** The user may create a fresh ticket. | monitor |
| `needs_review` | `confirmed` | Several or mismatching tagged orders, or the order book no longer covers the submission day. **Final for automation.** | monitor |

**Why the status stays `confirmed` while unresolved.** After PK-1, `confirmed` already means "execution claimed". Keeping it there gives safe behaviour everywhere without touching other modules:

- confirm and reject accept only other statuses → 409;
- edit accepts only `draft` → 409;
- delete/archive treat `confirmed` as permanent → 409;
- Cancel All Pending ignores it;
- the strategy engine already treats `confirmed` as in flight, for both entry de-duplication and `_has_pending_exit`, so a scan cannot propose a duplicate entry or exit.

How each required case is distinguished:

| Case | How to tell |
|---|---|
| Never submitted | `submission_state IS NULL` and status not `executed` |
| Broker order known and working | `executed` + `entry_fill_status='pending'` |
| Filled | `entry_fill_status='filled'` |
| Rejected / cancelled | `failed/rejected`, or `cancelled` with the broker status in `entry_fill_status` |
| Outcome unknown | `confirmed` + `unknown` (or a stale `submitting`) |

## 5. Exception classification

Verified in the installed kiteconnect 5.0.1 (`connect.py`, `_request` and `place_order`):

- `reqsession.request` is wrapped in `except Exception as e: raise e`, so raw `requests` exceptions escape unwrapped.
- A JSON parse failure or a non-JSON content type raises `DataException`.
- An error body raises `getattr(ex, error_type, GeneralException)`.
- `place_order` then indexes `["order_id"]`, so a malformed success body raises `KeyError`/`TypeError`.

Only the `place_order` call is inside the `try`. Kwarg building and tick rounding were moved out of it, so everything caught happened after the request may have left the process.

| Exception from `place_order` | Classification | Why |
|---|---|---|
| `OrderException` | **rejected** | Explicit RMS/order error body from Kite |
| `InputException` | **rejected** | Explicit validation error body |
| `PermissionException` | **rejected** | Explicit permission error body |
| `NetworkException` | unknown | Kite's own gateway/OMS timeout: the order may have been accepted upstream |
| `TokenException` | unknown | Treated conservatively, as the packet requires |
| `GeneralException` | unknown | **Changed.** Was "rejected". Kite raises it for a 5xx body or an unrecognised `error_type`, which is not proof of rejection. |
| `DataException` | unknown | Unparseable or non-JSON body: the request was processed to some extent |
| `requests.ReadTimeout`, `requests.ConnectionError`, and every other `requests` error | unknown | Previously **uncaught → HTTP 500 and a stranded ticket** |
| `KeyError`, `TypeError`, `ValueError` (malformed response) | unknown | Previously uncaught |
| Any other exception from `place_order` | unknown | Cannot prove rejection |
| An exception escaping `execute_ticket` on a live ticket | unknown | Caught in `confirm_and_execute_ticket`: it cannot tell how far execution got |

`ConnectTimeout` (the connection never opened) is also classed unknown. It is a subclass of `ConnectionError`, and the packet asks for the conservative choice.

## 6. Order tag format

`execution.order_tag_for(ticket_id, created_at)` → **`kt<ticket_id>x<created_at as base36 Unix seconds>`**, for example `kt42xtm0fe0`.

- **Kite's rules.** Alphanumeric, at most 20 characters; both are enforced with a `ValueError`. A 10-digit ticket id still fits: `kt9999999999x1vuhmnz` is 20 characters.
- **Deterministic and stable across restart.** It is derived only from the ticket row, and is also persisted in `trade_tickets.order_tag` at claim time.
- **Unique enough.** The `created_at` suffix protects against the one realistic collision in this project. Restoring a database backup reissues AUTOINCREMENT ids, and a bare `kt<id>` could then match an order placed the same day by an earlier ticket with the same id.
- **Used for both entry orders (`_execute_live`) and exit orders (`_execute_exit`).** GTTs cannot be tagged in Kite's API.

## 7. Unknown-order recovery algorithm

It runs in `order_monitor.check_pending_orders` every 20 s, before the pending-order loop.

1. **Select** (`unresolved_submissions`) live tickets with `status='confirmed'` and either `submission_state='unknown'`, or `'submitting'` with `submitted_at` older than `SUBMISSION_STALE_SECONDS`. A younger `submitting` claim may still be inside `place_order` and is left alone.
2. **Fetch** `kite.orders()` **once**. If the fetch fails, the cycle is skipped and nothing is concluded.
3. **For each ticket** (`_reconcile_submission`):
   1. If it is a stale `submitting` claim: compare-and-set `submitting → unknown`, log `submission_unknown` ("lost control mid-submission") and send an urgent broadcast. If the compare-and-set loses, the request has recorded its outcome after all; stop.
   2. **Tag search.** Match on `order["tag"] == order_tag` or `order_tag in order["tags"]`.
   3. **Exactly one match, and it agrees** with the ticket's symbol, exchange, side and quantity → `_adopt`.
   4. **More than one match**, or **one match that disagrees** → `_needs_review`.
   5. **No match:**
      - within `UNKNOWN_ORDER_RECONCILE_WINDOW_SECONDS` of `submitted_at` → no write; keep searching;
      - after the window, with `submitted_at` and now on the **same IST day** → `_not_found`;
      - otherwise (a different day, or no `submitted_at`) → `_needs_review`. The order book no longer covers the submission, so absence proves nothing.
4. Then the ordinary pending-order loop runs. **An order adopted in step 3 is picked up in the same cycle.**

Every resolution is a compare-and-set from `confirmed/unknown` (`_set_unresolved`), so it is forward-only and happens at most once. Recovery never calls `place_order`, `cancel_order` or `place_gtt`.

**`needs_review` evidence.** A `submission_unknown` event with the reason, the tag and every tagged order (id, side, quantity, symbol, status); a `reconciliation_log` row (`order_status`, expected "exactly one broker order tagged T for BUY 5 TCS", actual listing the order ids); and one urgent broadcast. It is logged once: later cycles no longer select the ticket.

## 8. Recovery timeout policy and rationale

| Setting | Value | Rationale |
|---|---|---|
| `SUBMISSION_STALE_SECONDS` | 120 s | The confirm path makes at most three sequential Kite calls (`place_order`, `ltp`, `place_gtt`), each with kiteconnect's 7 s timeout: about 21 s. 120 s cannot race a live request, and still recovers a crashed one within two minutes. |
| `UNKNOWN_ORDER_RECONCILE_WINDOW_SECONDS` | 900 s (15 min) | Kite lists accepted, AMO and RMS-rejected orders in the order book within seconds. 15 minutes is about 45 monitor cycles, a deliberately large margin before absence is treated as evidence. |
| Same-IST-day rule | required for `not_found` | The order book covers the current session only. Across a day boundary (for example, the app was down overnight), "not found" is no longer evidence, so the ticket goes to `needs_review`. |
| Successful fetch | required | A failed `orders()` call skips the cycle, so a broker outage during the window cannot produce `not_found`. |

## 9. Entry recovery behaviour

**On adoption** (`_adopt`), the compare-and-set sets:

| Column | Value |
|---|---|
| `status` | `executed` |
| `kite_order_id` | the broker's id |
| `order_variety` | from the broker order |
| `executed_at` | `submitted_at` (when the order was actually sent) |
| `current_stop_loss` | `stop_loss` |
| `entry_fill_status` | `pending` |
| `submission_state` | `adopted` |

An `executed` event records "RECOVERED … Nothing was resubmitted".

**Protection.** The request lost control before it could place the protective GTT, so `kite_gtt_id` is NULL.

- **The monitor does not create a GTT.** A GTT carries no tag, so placing one is not idempotent. If the original request did in fact place a GTT (the late-result race in section 12), a monitor-created one would duplicate protection. Two SELL triggers against one position would risk an oversell.
- **Instead, the existing unprotected behaviour is surfaced** at adoption: a `protection_failed` event and an urgent broadcast, "recovered entry order … has NO protective GTT. UNPROTECTED POSITION once it fills — place a stop-loss in Kite now." The UI already flags executed live rows without a GTT.
- **This is skipped when the adopted order is already `REJECTED`/`CANCELLED` with no fill,** because nothing will ever fill.

**Order states after adoption** go through the unchanged lifecycle in the same cycle:

- `OPEN`, `TRIGGER PENDING`, `AMO REQ RECEIVED` → waits;
- `REJECTED`/`CANCELLED` → `cancelled`;
- partial → `partial` + mismatch.

**On `COMPLETE`** (`_resolve_entry`), for an `adopted` ticket that uses GTTs, protection is **verified, never assumed**. `_gtt_is_active` checks `kite_gtt_id` against `get_gtts()`; a failed check counts as "not protected". If it is not active:

- a second `protection_failed` event;
- `reconciliation.log_mismatch(position_state, expected="filled position protected by an active GTT")`;
- an urgent broadcast "FILLED with NO active protective GTT".

The position is never marked protected.

## 10. Exit recovery behaviour

**While the outcome is unknown,** `_execute_exit` returns **before** deleting the original position's GTT. That GTT is the position's only protection until the SELL is known to exist. The position stays open (`closed_at` NULL), and nothing is journaled.

**On adoption**, the exit ticket gets the same columns as an entry. The monitor then does **exactly what the confirm path does today** once it has the order id (current, P0-7-defective semantics, deliberately not changed):

1. delete the original GTT (`execution.cancel_original_gtt`, extracted from `_execute_exit`);
2. stamp the original's `closed_at`;
3. write the closed_trades row at the reference price (`journal.close_position_for_exit`, extracted from the confirm path).

The fill then re-prices the journal row through the unchanged `_resolve_exit`, in the same cycle when the order is already `COMPLETE`.

**If the adopted SELL is already `REJECTED`/`CANCELLED` with no fill,** steps 1–3 are skipped. The unchanged `_resolve_exit` then records the rejection with the position still open and its GTT still active. This avoids deleting protection for a SELL already known to have failed, and changes nothing about how a *placed* exit behaves.

**Not found:** `failed/not_found`, with "The position is still open and its protective GTT was left in place."

**Emergency Exit All** (`kill_switch.py`) no longer creates a second SELL for a position that already has an exit in `confirmed`, whether in flight, unknown or needs_review. It reports `status: 'exit_outcome_unknown'` for that position. This was needed for "never create another order while the outcome is unknown", because Emergency Exit otherwise ignores existing exits (P0-4B). The strategy exit scanner was already safe via `_has_pending_exit`.

The PK-8 xfails (`test_p0_7_…[limit_market_hours]`, `[market_amo_after_hours]`) still xfail. The normal exit path is unchanged.

## 11. Process-restart behaviour

- **Everything recovery needs is in the database.** `order_tag`, `submission_state='submitting'` and `submitted_at` are written in the PK-1 claim transaction (together with `status='confirmed'`, `mode` and the `confirmed` event), **before** `execute_ticket` runs.
  - **Process dies during `place_order`:** the row stays `submitting`, and after 120 s the monitor treats it as unknown.
  - **Process dies after the request recorded `unknown`:** the monitor picks it up on its first cycle after restart.
- **No in-memory state.** The monitor holds nothing between cycles: its inputs are the row and `kite.orders()`. `test_p0_2_restart_adopts_tagged_order_from_database_alone[unknown|submitting]` writes the row directly, seeds the broker order with `add_broker_order` (no request, no `place_order` call), and calls a fresh `check_pending_orders`.
- **Legacy rows.** A pre-PK-2 live ticket stranded in `confirmed` has no tag and `submission_state IS NULL`, so it is not selected: there is nothing to search for. The real database (as of the backup) has **no** such ticket.

## 12. Conditional writes added because order_monitor is now a second writer

| Writer | Write | Condition |
|---|---|---|
| confirm | claim (PK-1 `_transition`, now with the submission columns) | `status='pending_confirmation'` |
| confirm | executed / protection_failed result | `status='confirmed' AND submission_state IN ('submitting','unknown')` (live), `IS NULL` (dry run). Definite broker evidence may still upgrade an `unknown` the monitor set on a stale claim. |
| confirm | definite rejection | same as above |
| confirm | unknown | `status='confirmed' AND submission_state='submitting'` |
| confirm | late GTT attach (only after losing) | `kite_order_id = <this request's order> AND kite_gtt_id IS NULL` |
| monitor | stale `submitting → unknown` | `status='confirmed' AND submission_state='submitting'` |
| monitor | adopt / not_found / needs_review | `status='confirmed' AND submission_state='unknown'` |
| monitor | fill / partial / cancelled / failed / unconfirmed (`_claim_pending`) | `status='executed' AND entry_fill_status='pending'`, done **before** any broker side effect (the entry-cancel path now updates status before deleting the GTT) |

All return `rowcount == 1`, and the loser does not write.

**When the confirming request loses** (`_reconcile_late_result`):

- the monitor's state stands;
- the request re-reads the ticket and records its broker result as a `modified` event;
- if it holds an order id the ticket does not track, it also writes a `reconciliation_log` mismatch;
- the one thing it may add is the GTT it placed for the very order the monitor adopted (conditional attach);
- exit closure runs **only for the writer that won**, so the journal is never written twice;
- the response reports the current row, with a message explaining that the monitor resolved it first.

**No state ever moves backwards,** and nothing returns to `pending_confirmation`.

## 13. UI changes

Minimal, as requested. The larger lifecycle UI is PK-19.

- **Badges** (`statusBadgeHtml`):
  - `confirmed`+`unknown` → **"UNKNOWN — DO NOT RESUBMIT"**;
  - `confirmed`+`needs_review` → **"NEEDS REVIEW — CHECK KITE"**;
  - both use the new `.status-badge.submission_unknown` style: a dashed amber outline, bold and not capitalised. It is visually distinct from grey rejected/cancelled and red failed/risk-rejected;
  - `failed`+`not_found` → "not placed · no broker order" (failed style; it is final).
- **Confirm toast** (`reportExecution`): an unresolved outcome shows a sticky warning, "… — outcome UNKNOWN, do not resubmit". It never shows "not executed" or "Approval failed".
- **Ticket rows** with unknown or needs_review are flagged (`row-flagged`), like an unprotected position.
- **The detail dialog** shows a "Kite order tag" row.
- **Actions.**
  - Approve is only offered for `pending_confirmation`, both in the confirm dialog (PK-1) and in Signal Detail.
  - For `confirmed`, the detail dialog shows no Edit, Delete, Archive or Revert, and the backend returns 409 for each.
  - A `not_found` ticket is archivable. Archiving is a soft flag and its events remain.

**Verified in a browser.** `tests/demo_server.py` was run with a scratch workdir, the three states were seeded, and Playwright/Edge was used to check:

- both unknown-style badges render with the new class;
- the not-found badge renders;
- the rows are flagged;
- the dialog shows the tag;
- all four action buttons are hidden.

## 14. Tests converted from xfail

| PK-0 test | Now | Strengthened with |
|---|---|---|
| `test_p0_2_entry_raw_network_error_strands_ticket[ReadTimeout]` | renamed `test_p0_2_entry_raw_network_error_is_unknown_not_stranded[ReadTimeout]` | `confirmed/unknown`, tag and `submitted_at` persisted, 1 `place_order` after background cycles, `submission_unknown` event |
| `…[ConnectionError]` | renamed likewise | same |
| `test_p0_2_entry_accepted_but_response_lost` | parametrised `[ReadTimeout]` (requirement B) and `[ConnectionError]` (requirement C) | unknown response; second confirm 409; monitor adopts the one broker order; 1 `place_order`, 0 `place_gtt`; `protection_failed` + urgent broadcast |
| `test_p0_2_exit_accepted_but_response_lost` | requirement F | original GTT still `active` and position open while unknown; SELL adopted; exactly one SELL. No P0-7 assertion. |

`test_p0_2_kite_network_exception_reports_unknown` (already passing) still passes unchanged.

## 15. New tests

All are in `tests/test_live_safety.py`, P0-2 section. They use FakeKite only, with no sleeps and no network.

| Test | Requirement |
|---|---|
| `test_p0_2_orders_carry_deterministic_kite_tag` | Entry and exit orders carry `tag == order_tag == order_tag_for(row)`, match `kt\d+x[0-9a-z]+`, and are ≤ 20 characters |
| `test_p0_2_broker_exception_classification[×10]` | Exception table: Order/Input/Permission → `failed/rejected`; Network/Token/General/Data/ReadTimeout/ConnectionError/KeyError → `confirmed/unknown`, HTTP 200 |
| `test_p0_2_timeout_before_acceptance_reaches_not_found_after_window` | **A.** Unknown through cycles at +1, +10 and +14:59 minutes, with the order book searched each time; `failed/not_found` at +15:00; 1 `place_order`; confirm → 409 |
| `test_p0_2_unknown_from_an_earlier_day_needs_review_not_not_found` | **A** (day-boundary variant) |
| `test_p0_2_restart_adopts_tagged_order_from_database_alone[unknown\|submitting]` | **D.** An unrelated, differently tagged order is ignored |
| `test_p0_2_in_flight_submission_is_left_alone` | A young `submitting` claim is not touched |
| `test_p0_2_adopted_entry_fill_continues_lifecycle_and_flags_missing_protection` | **E.** Adopted while OPEN; COMPLETE → `filled` at the broker's average price; `kite_gtt_id` stays NULL; urgent alert and `position_state` mismatch |
| `test_p0_2_unknown_ticket_stays_non_actionable_while_searching` | **G.** Confirm, reject, edit and delete → 409; Cancel All cancels 0; still unknown after 3 rounds of every background loop |
| `test_p0_2_ambiguous_tagged_orders_need_review[two_tagged_orders\|tagged_order_mismatch]` | **H** |
| `test_p0_2_race_monitor_adopts_before_request_records_unknown` | **I(1)** |
| `test_p0_2_race_request_result_after_adoption_attaches_gtt_without_overwriting` | **I(2)** |
| `test_p0_2_race_request_result_cannot_clear_needs_review[unknown\|order_id_returned]` | **I(3).** The race that only the `submission_state` condition catches (status stays `confirmed`) |
| `test_p0_2_race_two_monitors_adopt_exactly_once` | **I(4)**, for example two app processes |
| `test_p0_2_race_two_monitors_resolve_a_fill_exactly_once` | The monitor's own `_claim_pending` guard |
| `test_p0_2_emergency_exit_does_not_resubmit_an_unknown_exit` | Kill-switch guard |

**How the race tests stay deterministic.** They use PK-1's technique: they monkeypatch `execution.update_ticket_if` (or `order_monitor._claim_pending`). The first call runs the competing actor to completion on its own connection, on the same thread, before the write executes. The races that need a stale claim set `config.SUBMISSION_STALE_SECONDS = 0`, and the window tests pin `order_monitor._utc_now` and `submitted_at` to fixed timestamps.

**Mutation checks.** Each file was copied first, mutated, tested, restored from the copy, and the restore was verified.

| Mutation | Result |
|---|---|
| `update_ticket_if` ignores `submission_state` (`AND (1 OR …)`) | **2 failures:** `race_request_result_cannot_clear_needs_review[unknown\|order_id_returned]`. This test was added *because* the first mutation run showed no existing test caught it. |
| Tag filter removed (all orders match) | **2 failures:** `restart_adopts_tagged_order…[unknown\|submitting]` |
| `_claim_pending` guard removed | **1 failure:** `race_two_monitors_resolve_a_fill_exactly_once` |
| `update_ticket_if` ignores `status` | **Not detected.** For a live ticket every post-claim state has its own `submission_state` value, which determines the status, so the status condition is redundant defence in depth. It is kept deliberately and reported here rather than covered by a contrived test. |

## 16. Full test results

Command: `.venv/Scripts/python -m pytest` (the repository `pytest.ini` adds `-q`).

| Run | passed | xfailed | failed | xpassed | time |
|---|---|---|---|---|---|
| Baseline (before any edit) | 133 | 17 | 0 | 0 | 24.31 s |
| 1 | 165 | 13 | 0 | 0 | 28.82 s |
| 2 | 165 | 13 | 0 | 0 | 28.92 s |
| 3 | 165 | 13 | 0 | 0 | 28.82 s |

**How the counts reconcile.** 133 + 4 converted + 28 new cases = 165, and 17 − 4 = 13. The P0-2 section now has 33 cases: 5 before PK-2 (1 passing and 4 xfail), plus 28.

## 17. Repeated targeted-test results

Command: `pytest tests/test_live_safety.py -k "p0_2 or p0_1 or p0_4a"`. This covers all PK-2 tests and all PK-1 atomic-transition tests.

**20 consecutive runs: 48 passed each time**, with 0 failures and run times of 8.01–8.32 s.

## 18. Remaining P0 xfails (13, all strict, none XPASS)

| Finding | Packet | Tests |
|---|---|---|
| P0-3 dry-run applied to a live position | PK-3 | `emergency_exit_in_dry_run…`, `exit_ticket_confirmed_in_dry_run…`, `breakeven_in_dry_run…` |
| P0-4B stale exit | PK-4 | `stale_exit_cannot_sell_already_closed_position` |
| P0-4C joint limit | PK-4 | `pending_entries_cannot_jointly_exceed_open_position_limit` |
| P0-5 OCO closure | PK-6 | `oco_stop_or_target_fill…[stop]`, `[target]` |
| P0-6 GTT trigger without fill | PK-7 | `triggered_gtt_without_fill…[unfilled]`, `[partial]` |
| P0-7 exit placement vs fill | PK-8 | `unfilled_exit_keeps_protection…[limit_market_hours]`, `[market_amo_after_hours]` |
| P0-8 CSRF | PK-5 | `cross_site_post_is_rejected[confirm]`, `[gtt_cancel]` |

## 19. Known limitations intentionally deferred

- **No automatic protective GTT for a recovered entry.** This is deliberate (section 9): the user is alerted twice, at adoption and at fill. Idempotent GTT creation needs broker-side identification of GTTs, which is P2 work alongside the `broker_orders` model.
- **`needs_review` has no in-app resolve action.** The operator resolves it in Kite. The ticket stays `confirmed`: visible, urgent, and never re-executable. It is also still counted as in flight by the strategy engine, which blocks re-proposal for that symbol and strategy (conservative). A resolve/close action belongs to PK-19 or the P2 lifecycle model.
- **Partial fills** of an adopted order go through the existing (partial) partial-fill handling, unchanged.
- **Legacy stranded tickets** (pre-PK-2 `confirmed`, no tag) are not recovered; there are none in the real data.
- **The deferred items were not touched:** dry/live exit mode (PK-3), re-validation at confirm (PK-4), stale exits (PK-4; only the unknown-exit case is guarded in Emergency Exit), the OCO watcher (PK-6), GTT-trigger-without-fill (PK-7), exit placement vs fill (PK-8), CSRF (PK-5), sector mapping, account safety controls and the multi-account architecture.
- **`GeneralException` is now unknown.** The real database's four live `failed` tickets were Kite's "No IPs configured for this app" error. It was logged as "rejected by Kite", so it came through one of `OrderException`/`InputException`/`GeneralException`/`PermissionException`. **If Kite sends that error as `GeneralException`,** such a ticket will now show UNKNOWN for 15 minutes and then resolve to `not_found`, instead of failing immediately. That is safe, but noisier. If Kite sends it as `PermissionException`, nothing changes. This could not be verified offline.

## 20. Findings that change PK-3 or PK-4 assumptions

1. **(PK-3) Mode and submission tracking are fixed at claim.** Submission fields are only written when the claim's mode is `live`. PK-3's "an exit of a live position must be live" change must decide the mode **before** the claim, and pass it to `_transition`, so that the submission fields and the tag are written for that exit.
2. **(PK-4) Exposure ignores unresolved entries.** `risk_engine` counts only `executed` tickets as open positions and exposure. An entry in `confirmed/unknown` or `needs_review` may be live at the broker, but is invisible to `max_open_positions`, single-stock exposure and sector checks until it is adopted. The joint-limit fix (P0-4C) should count `status='confirmed'` live tickets as potential exposure.
3. **(PK-4) Emergency Exit and unresolved entries.** An unresolved **entry** is not in `list_open_positions`, so EXIT ALL neither exits nor cancels it: it has no known order id to cancel. Once adopted, it is handled normally.
4. **(PK-4) Emergency Exit now skips a position with an exit in `confirmed`,** reporting `exit_outcome_unknown`. The P0-4B fix, which is about a *pending_confirmation* stale exit, should keep this guard and extend it rather than replace it.
5. **(PK-4) The shared-connection rollback note from PK-1 now applies twice.** `confirm_and_execute_ticket` also calls `conn.rollback()` when its outcome write loses. As before, nothing of Emergency Exit's own is uncommitted at that point, but a later packet that adds uncommitted writes before the confirm call would lose them.
6. **(PK-4) `update_ticket_if` is the reusable compare-and-set for any post-claim write.** The remaining unconditional writers PK-1 listed (`risk_engine.run_risk_checks_and_transition`, `edit_ticket`, `revert_rejection`, `delete_ticket`) can use `_transition` / `update_ticket_if` directly.
