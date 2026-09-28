# OPUS55 PK-17 Implementation Report: restrict new positions to the supported CNC long lifecycle (P1-11)

| | |
|---|---|
| Packet | **PK-17** only (audit **P1-11**). PK-18 and later were not started. |
| Baseline before | **761 passed**, 0 failed, 0 xfailed, 0 xpassed, on the untouched code before the first edit (two runs). |
| Result after | **809 passed, 0 failed, 0 xfailed, 0 xpassed** on every full run completed so far (2). The ×3 run was cut short by a low-memory stop; see *Full Suite ×3*. 809 = 761 + 48 new tests in `tests/test_supported_trade_lifecycle.py`. One existing test was restated (see *Deviations*). |
| Backups taken first | Tag `backup-pre-pk17` and branch `backup/pre-pk17`, both at `2c0bb0c` (the tree was clean). `backend/kite_terminal.db.pre_pk17_bak` is a file copy of the live DB, taken while the WAL was empty (0 bytes). |
| Live DB | Not written. PK-17 adds no migration. Tests use a per-test temporary DB and FakeKite, with no real Kite or network calls. The historical-ticket scan below read the **backup**, opened `mode=ro&immutable=1`. |

# Executive Summary

**Before PK-17,** a manual ticket could be any of BUY/SELL × CNC/MIS/NRML (`TicketCreateRequest`, `TicketEditRequest`).

A manual **SELL** has no `closes_ticket_id` and is not `source='alert_action'`, so `execution.is_closing_sell()` is False for it. It was therefore treated as *opening a short*:

- the six position-opening risk checks ran;
- `_execute_live` placed a SELL order;
- `_execute_live` then armed a protective **BUY** GTT.

For CNC this either fails at the broker, or sells holdings from outside the app and arms a buy-back GTT. MIS/NRML auto square-off, margin and product conversion are not modelled anywhere. The journal, open positions, exits and protective GTTs are all long-only (`list_open_positions` filters on `transaction_type='BUY'`). The audit finding was confirmed against the current code before any edit.

**Now a NEW position must be `BUY` + `CNC`.** Anything else is **refused, never rewritten**, with the stable machine-readable code `UNSUPPORTED_MANUAL_TRADE`. The rule is written once (`execution.unsupported_entry_message` / `unsupported_new_position`) and enforced at four layers:

| Layer | Where | Outcome for an unsupported new position |
|---|---|---|
| 1. Creation / edit | `routes/tickets.py:_refuse_unsupported_entry`, first thing in `_create_ticket_internal` (manual + Quick Action buy) and in `edit_ticket` | **400** `UNSUPPORTED_MANUAL_TRADE`; nothing inserted or updated, no instrument lookup, no broker call |
| 2. Risk engine | `risk_engine.run_checks`, before `build_portfolio_context` | one failed `supported_lifecycle` check, so the ticket goes to `risk_rejected`; no sizing/exposure work, no broker call |
| 3. Confirmation | `routes/tickets.py:_refuse_unsupported_at_confirm`, before the mode binding, the claim and the account-safety gate | **409** `UNSUPPORTED_MANUAL_TRADE`; the ticket goes to `rejected` with an event; nothing claimed or sent (dry-run and live) |
| 4. Execution backstop | `execution.execute_ticket`, its first statement, before the dry-run branch | a definite failed `ExecutionResult` (`execution_failed`, message starts with `UNSUPPORTED_MANUAL_TRADE:`); no broker call |

**Closing sells are untouched.** This covers linked exits, Emergency Exit, GTT-closure rows and Quick Action sells of held shares. The UI now offers only BUY / CNC for new entries, with one short note.

**Validation:**

- 48 new tests;
- 9/9 focused mutations caught (4 required + 5 extra), all files restored and hash-verified;
- the PK-17 suite ×10 and the execution/exit regression subset ×10, all green;
- full suite: 809 passed on each of the 2 completed runs; the third run is still outstanding (a low-memory stop, not a failure).

# Supported Trading Lifecycle

One lifecycle is modelled end to end:

```
BUY (CNC) entry ──> protective SELL GTT (OCO stop/target, or single stop when trailing)
      │
      └── closed by a SELL: strategy exit · Emergency Exit · Quick Action sell · GTT auto-trigger
```

| Ticket kind | transaction_type | product_type | How it is recognised |
|---|---|---|---|
| New position (manual, Quick Action buy, strategy entry) | `BUY` | `CNC` | `not is_closing_sell(ticket)` |
| Linked exit (strategy exit, Emergency Exit, alert-action full close, GTT closure) | `SELL` | inherited from the position | `closes_ticket_id IS NOT NULL` |
| Holding reduction (Quick Action sell, partial or untracked) | `SELL` | from the broker holding (`CNC`) | `source = 'alert_action'` |

# Unsupported Lifecycles

Refused for NEW positions, and **not implemented** in PK-17:

- **Short selling:** SELL to open, BUY to cover, borrow/SLB, and short-side GTT logic.
- **MIS intraday:** auto square-off around 15:20, intraday leverage/margin, and restart handling for a squared-off position.
- **NRML:** carry-forward, derivatives lifecycle, and broker product conversion.
- GTT/protection compatibility for any non-CNC product.

The five refused combinations are SELL+CNC, BUY+MIS, SELL+MIS, BUY+NRML and SELL+NRML.

# Server Validation

`backend/execution.py`:

```python
UNSUPPORTED_MANUAL_TRADE = "UNSUPPORTED_MANUAL_TRADE"
SUPPORTED_ENTRY = ("BUY", "CNC")

def unsupported_entry_message(transaction_type, product_type) -> str | None   # None = supported
def unsupported_new_position(ticket_row) -> str | None                         # None for any closing sell
```

**Creation and edit (400).** `_refuse_unsupported_entry(req)` runs **before** `_validate_prices`, the instrument lookup, the INSERT/UPDATE and the risk checks. There is nothing to size or expose for a trade the terminal cannot manage. Response body:

```json
{"detail": {"error_code": "UNSUPPORTED_MANUAL_TRADE",
            "message": "Unsupported new position: SELL + CNC. Manual entries currently support CNC long positions only (BUY + CNC). Use Exit / Quick Sell to close holdings.",
            "transaction_type": "SELL", "product_type": "CNC"}}
```

- 400 matches the existing `VALIDATION_ERROR` / `INSTRUMENT_NOT_FOUND` convention and the audit's "SELL/MIS → 400".
- The route also serves `POST /api/tickets/alert-action/buy`, which reuses `_create_ticket_internal`, so a Quick Action BUY + MIS is refused too.
- `PUT /api/tickets/{id}` runs the same check, so a supported draft cannot be edited into a short or an intraday entry.
- **Pydantic `Literal`s were deliberately left wide** (with a comment in `models.py`). Narrowing them to `Literal["BUY"]` / `Literal["CNC"]` would give a generic FastAPI 422 with no stable code. The server-side rule is the one enforced, and it is not the only layer.

**Risk engine.** `run_checks` returns one failed `CheckResult("supported_lifecycle", "Supported Trade Lifecycle", …, code="UNSUPPORTED_MANUAL_TRADE")` **before** `build_portfolio_context`. This covers every ticket that reaches risk checks without passing through the route: strategy tickets, reverted rejections and older rows. The frontend renders the message through its existing `actual_value is None` path.

# Execution Backstop

`execute_ticket` starts with:

```python
unsupported = unsupported_new_position(ticket)
if unsupported is not None:
    return ExecutionResult(success=False, ..., status="failed", event_type="execution_failed",
                           message=f"UNSUPPORTED_MANUAL_TRADE: {unsupported} Refused before any broker call; nothing was sent.")
```

- It runs **before the dry-run branch**, so an unsupported ticket is not even simulated.
- It **returns a definite failure rather than raising.** In `confirm_and_execute_ticket` an exception from a live execution becomes an UNKNOWN submission (`unknown_result`), which `order_monitor` would then search for by tag. In dry run the exception would propagate as a 500 and leave the ticket stuck at `confirmed`. A failed result is recorded by `_record_execution_result` like any definite broker rejection: the status becomes `failed`.
- **Side effect worth knowing:** for a live claim the ticket's `submission_state` is set to `rejected`. That value normally means "the broker definitively refused". The event message makes clear the refusal was local and that no order left the process.

In normal operation the backstop is unreachable: layers 1–3 stop the ticket first. It exists for a row that reaches execution by some path that skipped them (an older version, a manual DB edit, a bad migration, a future caller). Mutation M4 shows the direct execution-layer tests depend on it.

# SELL Exit Distinction

`execution.is_closing_sell()` is still the single source of truth, and `unsupported_new_position()` returns `None` for any ticket it accepts:

| SELL kind | `closes_ticket_id` | `source` | PK-17 |
|---|---|---|---|
| **NEW SELL** (manual short) | NULL | `manual` / `strategy` | **refused** at every layer |
| Strategy exit | position id | `strategy` | allowed |
| Emergency Exit | position id | the position's own | allowed |
| GTT auto-trigger closure | position id | position's | allowed (inserted `executed`, never reaches `execute_ticket`) |
| Quick Action full close | position id | `alert_action` | allowed |
| Quick Action partial / untracked reduction | NULL | `alert_action` | allowed |

The product of an exit is never checked. An exit of a legacy MIS/NRML position is still allowed, because closing risk must always stay possible. A test covers this: `test_exit_of_a_legacy_intraday_position_is_not_blocked`.

# Quick Action / Alert Sell Behavior

The Quick Action sell (`POST /api/tickets/alert-action/sell`) is **preserved unchanged**. It deliberately reduces holdings that may have no originating ticket (bought before the app, or directly in Kite), so it is not forced into `closes_ticket_id`. It cannot open a short:

1. **At creation**, the route refuses `NOT_HELD` if the symbol is not in `kite.holdings()`, and `EXCEEDS_HELD_QUANTITY` if the quantity exceeds `risk_engine.get_held_quantity`.
2. **At risk check**, `_check_exit_or_reduction` case (b) re-validates `0 < quantity <= held quantity` against the broker.
3. **At confirmation**, `_revalidate_at_confirm` runs the same check again against the broker's holdings.
4. The product is taken from the broker holding (`CNC` for delivery holdings) and never from the client. A reduction never arms a GTT of its own (`_execute_exit`), so no BUY-back GTT can appear.

The **Quick Action buy** uses the same body as a manual entry, so PK-17 applies to it in full.

# Strategy Interaction

- **Strategy entries** are hard-coded `'BUY'` and `'CNC'` in `strategy_engine._create_entry_ticket`. Behaviour is unchanged, and `test_strategy_buy_cnc_entry_still_passes_risk` pins it.
- **Strategy exits** set `closes_ticket_id` and inherit the position's product. They are unchanged.
- **Future-proof guard:** strategy tickets bypass the HTTP route, but they all pass through `risk_engine.run_risk_checks_and_transition`. A misconfigured strategy that produced SELL, MIS or NRML would get `risk_rejected` with the `supported_lifecycle` check, and **no broker call** would be made (the test asserts FakeKite recorded nothing). If such a ticket were somehow approved, layers 3 and 4 would still refuse it.
- Strategy logic itself was not modified.

# Historical Ticket Behavior

No historical row is rewritten or deleted. A read-only scan of the PK-17 backup (18 tickets) found exactly **one** unsupported new position: **#9, manual BUY + NRML, status `failed`**. It is terminal and cannot be approved. None is pending.

What happens to an unsupported row in each state:

| State | What happens |
|---|---|
| `draft` | Editing is refused (400) unless the edit makes it BUY + CNC. Deleting a draft works as before. |
| `pending_confirmation` | **Approve** is refused with `409 UNSUPPORTED_MANUAL_TRADE`. The ticket is moved to `rejected` (compare-and-set) with the event *"Refused at confirmation (UNSUPPORTED_MANUAL_TRADE): … No order was sent."* It is never claimed (`submission_state`/`order_tag` stay NULL), never sent, and kept for the record. This applies in dry-run and in live mode. |
| `rejected` → Revert | The risk checks re-run and the ticket lands in `risk_rejected` with the `supported_lifecycle` check. It can never become approvable. |
| `confirmed` (claimed by an older version) | If it ever reached `execute_ticket` again, the backstop returns `failed`. |
| `executed` (an old short or MIS position) | Not touched by PK-17. Its exits are still allowed. See *Known Limitations*. |

# UI Changes

Minimal changes, `frontend/index.html` only:

- The **Manual ticket** form: *Direction* offers only `BUY` and *Product* offers only `CNC`. They are kept as single-option selects, so the fields stay visible and `app.js` needs no payload change.
- The **Ticket edit** form: same.
- The **Quick Action buy** product: only `CNC`.
- One note above the Manual ticket form (`muted-note`): *"Manual entries currently support CNC long positions only. Use Exit / Quick Sell to close holdings."*
- SELL is reachable only through Close/Exit, Quick Action Sell and Emergency Exit.

If a legacy SELL/MIS draft is loaded into the edit form, the select has no matching option. The server then answers with the `UNSUPPORTED_MANUAL_TRADE` message, which appears through the form's existing error line.

# Tests Added

`tests/test_supported_trade_lifecycle.py`: 48 tests (the counts include parametrisation).

| # | Requirement | Test(s) |
|---|---|---|
| 1 | manual BUY + CNC accepted | `test_manual_buy_cnc_is_accepted_and_executes` (creates, confirms live, asserts a BUY/CNC order) |
| 2–6 | SELL+CNC, BUY+MIS, SELL+MIS, BUY+NRML, SELL+NRML refused | `test_unsupported_manual_entry_is_refused_at_creation` ×5 combos × {dry_run, live}: 400 + code, no row, **no Kite call** |
| — | refused, not rewritten | `test_unsupported_entry_is_refused_not_rewritten` |
| — | edit path | `test_draft_cannot_be_edited_into_an_unsupported_entry` ×3 (row byte-identical afterwards) |
| — | Quick Action buy | `test_alert_action_buy_with_intraday_product_is_refused` |
| 7 | linked SELL exit | `test_linked_exit_sell_still_executes` (live SELL/CNC order placed) |
| 8 | Emergency Exit | `test_emergency_exit_sell_still_allowed` |
| — | exit of a legacy MIS position | `test_exit_of_a_legacy_intraday_position_is_not_blocked` |
| 9 | strategy BUY + CNC | `test_strategy_buy_cnc_entry_still_passes_risk` |
| — | misconfigured strategy | `test_misconfigured_strategy_entry_is_risk_rejected_without_broker_work` ×3 |
| 10 | Quick Action holding sell | `test_quick_action_sell_of_held_shares_still_allowed` (live SELL 4 of 10, no GTT), `test_quick_action_sell_cannot_open_a_short` |
| 11 | historical pending ticket | `test_historical_unsupported_pending_ticket_cannot_execute` ×5 combos × {dry_run, live}; `test_reverting_a_rejected_unsupported_ticket_lands_in_risk_rejected` |
| 12 | direct execution-layer attempt | `test_execution_layer_refuses_an_unsupported_new_position` ×5 × {dry_run, live}: zero FakeKite calls |
| — | backstop does not over-block | `test_execution_layer_still_runs_supported_entries_and_exits` (BUY/CNC entry, MIS-product linked exit, alert-action reduction) |
| 13 | dry-run refused | the dry_run parameters of #2–6, #11 and #12 |
| 14 | stable error code/message | `test_error_code_and_message_are_stable` (the whole `detail` is asserted exactly) |

# Mutation Results

`pk17_mutations.py` (scratchpad, not committed) ran once on the final code. For each mutant it:

1. applied one exact-anchor edit, asserting the anchor was unique;
2. ran `tests/test_supported_trade_lifecycle.py`;
3. restored the original bytes.

| # | Mutation | File | Result |
|---|---|---|---|
| M1 | allow manual SELL (rule becomes "product is CNC") | `execution.py` | **caught**, 9 failed |
| M2 | allow MIS | `execution.py` | **caught**, 10 failed |
| M3 | allow NRML | `execution.py` | **caught**, 8 failed |
| M4 | remove the execution backstop | `execution.py` | **caught**, 10 failed (the direct execution-layer tests) |
| M5 | (extra) remove the creation check | `routes/tickets.py` | **caught**, 13 failed (the risk engine still rejects, but with 201 `risk_rejected`, not 400) |
| M6 | (extra) remove the edit check | `routes/tickets.py` | **caught**, 3 failed |
| M7 | (extra) remove the confirm-time gate | `routes/tickets.py` | **caught**, 11 failed (revalidation still refuses, but as `RISK_REVALIDATION_FAILED` after a claim) |
| M8 | (extra) remove the risk-engine guard | `risk_engine.py` | **caught**, 4 failed |
| M9 | (extra) closing sells no longer exempt | `execution.py` | **caught**, 5 failed (legacy-MIS exit, Quick Action sell, execution over-block test) |

Survivors: none. After the run, `restored=True` for all three files, and the SHA-256 values match the ones recorded just before it:

- `execution.py` `7daac9f35c58…`
- `routes/tickets.py` `de21aefba1e4…`
- `risk_engine.py` `b3d87f579f56…`

Two harness problems were fixed before the run above; neither touched the code under test:

- a CRLF anchor mismatch in `execution.py` (the assertion fired before anything was written);
- the subprocess resolved `python` to an interpreter without pytest. It now uses `sys.executable` and UTF-8 output.

No code changed after the mutation run.

Each layer is **independently** load-bearing. Removing any one of them fails tests, even though the other layers still stop the trade. The error surfaced would then be a different, less precise one.

# Targeted Repeats

`repeat.ps1` (scratchpad) ran on the final code.

- **PK-17 targeted suite ×10** (`tests/test_supported_trade_lifecycle.py`): every run **48 passed**, 0 failed (13.7–14.2 s).
- **Execution/exit regression subset ×10:** every run **392 passed**, 0 failed (67–131 s). It covers:
  - `test_exit_lifecycle` (PK-8 EXITING);
  - `test_emergency_exit_isolation` (PK-10);
  - `test_mode_binding` and `test_pk9_pk11_cross` (PK-3 mode inheritance, PK-9/PK-11);
  - `test_cancel_order` (PK-9 cancellation);
  - `test_account_safety` and `test_account_safety_integration` (PK-12);
  - `test_partial_fills` (PK-13);
  - `test_gtt_reconciliation` (PK-16);
  - `test_alert_action` (Quick Action);
  - `test_live_safety` (PK-0–PK-4 exit ownership).

No flakes were seen. PK-17 adds no threads, timers or shared state.

# Full Suite ×3

**Incomplete: the ×3 run did not finish.** `repeat.ps1 -Times 3 -Targets -rfEX` ran on the final code (after the mutation run):

| Run | passed | failed | xfailed | xpassed | time |
|---|---|---|---|---|---|
| 1 | 809 | 0 | 0 | 0 | 240.8 s |
| 2 | — | — | — | — | stopped by Claude Code because the machine was critically low on memory; not a test failure |
| 3 | — | — | — | — | not started |

One earlier full run of the same source (after the last source edit, before the repeats and mutations) also gave **809 passed**, 0 failed. The source files were not changed between that run and run 1: the mutation run restored them byte-for-byte (SHA-256 verified).

**PK-17 is therefore not committed yet.** The commit rule requires three consecutive full-suite passes.

# Known Limitations

- **Existing executed shorts or MIS/NRML positions** (none exist in the current DB) are not migrated, closed or re-protected. Their exits are allowed. Their journal and GTT handling stay whatever the long-only code does. No auto square-off is modelled.
- **A live backstop refusal records `submission_state='rejected'`,** the same value used for a broker rejection. The event text says the refusal was local. A dedicated state would need a schema change, which is out of scope.
- **The SELL branch of `routes/tickets.py:_validate_prices` is kept.** It is unreachable for create/edit now (the lifecycle check runs first) and was kept to stay minimal.
- The exact exchange is not restricted. NSE/BSE equity CNC is what the rest of the app assumes, and PK-17 constrains direction and product only.
- `account_safety.check_price_sanity` and `buffered_stop_price` keep their short-side branches (now dead for new entries). They were kept so a future short lifecycle does not have to rediscover them.

# Deferred Work

- A modelled short lifecycle: SELL to open, BUY-to-cover exits, a short-side journal, a BUY-leg protective GTT, borrow/SLB. Not PK-17.
- An MIS lifecycle: auto square-off, intraday margin, end-of-day reconciliation, and restart after square-off.
- NRML / derivatives lifecycle and product conversion.
- A dedicated `submission_state` for "refused locally before the broker".
- Lifecycle UI redesign (PK-19). Security/cloud hardening (PK-18). Neither was started.

# Deviations From Audit

- **Four layers instead of "models.py, routes/tickets.py, app.js".** The audit's row for PK-17 names the model, the route and the UI. This implementation adds the risk-engine guard (for strategy/reverted/older tickets), the confirm-time refusal (for historical pending tickets) and the execution backstop (defence in depth), as the packet brief required. The Pydantic model was **not** narrowed; see *Server Validation*.
- **Confirmation refuses with 409, creation with 400.** At confirmation the problem is the ticket's state, not the request body. This matches the other confirm-time refusals (`STOP_ALREADY_INVALID`, `EXIT_MODE_MISMATCH`).
- **Existing tests touched (2):**
  - `tests/test_account_safety.py::test_12d_short_entry_whose_stop_is_already_crossed_is_rejected` created a SELL + MIS entry over HTTP, which PK-17 now refuses. It was restated as a direct unit test of `account_safety.check_price_sanity` with a SELL row, so the PK-12D short-side stop branch keeps its coverage.
  - `test_12h_live_entry_without_a_decided_variety_never_reaches_the_broker` passes a hand-built ticket dict to `execute_ticket`. It gained `"product_type": "CNC"`, because the backstop reads the product before the variety check. Its assertion is unchanged.
- `docs/OPUS55_AUDIT_REPORT.md` was not modified.
