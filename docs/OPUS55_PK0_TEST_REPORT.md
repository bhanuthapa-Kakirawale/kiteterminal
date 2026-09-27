# PK-0 — Live-Safety Characterisation Tests: Report

| | |
|---|---|
| Packet | PK-0 from `docs/OPUS55_AUDIT_REPORT.md` (test-only) |
| Base revision | `13ae3f8` (branch `feature/signal-detail-and-strategy-revision`) |
| Backups taken first | tag `backup-pre-pk0-tests`, branch `backup/pre-pk0-tests`, `backend/kite_terminal.db.pre_pk0_tests_bak` (SQLite online-backup API, so WAL contents are included) |
| Application code changed | **None.** `backend/`, `frontend/`, `tests/conftest.py` and the audit report are untouched. |
| Committed | No |

Every P0 finding was re-checked against the code at `13ae3f8` before its test was written. Every one was then reproduced deterministically with the in-memory `FakeKite`. Nothing contacts Zerodha or the network.

---

## 1. Files changed

| File | Change |
|---|---|
| `tests/fake_kite.py` | Generic failure-injection and broker-state helpers (section 6). Existing behaviour is unchanged: the full pre-existing suite still passes. |
| `tests/test_live_safety.py` | **New.** 18 test functions, 24 test cases once parametrised. |
| `docs/OPUS55_PK0_TEST_REPORT.md` | **New.** This report. |

## 2. Tests added

All tests are in `tests/test_live_safety.py`.

Tests with no marker document behaviour that is **already safe** and must stay safe.

Tests marked `xfail(strict=True, raises=AssertionError)` pin a **current defect**:
- Only an `AssertionError` counts as the expected failure. An unrelated crash still fails the suite.
- Setup facts go through `_precondition()`, which raises `PreconditionError` rather than `AssertionError`. A broken scenario can therefore never hide behind an xfail.
- Each xfail asserts only properties that the audit's recommended fix would satisfy. When the fix lands, the test XPASSes and fails the run instead of staying silently xfailed.
- Each assertion collects a list of violations, so the failure message states the exact defective state.

| # | Test | Marker |
|---|---|---|
| 1 | `test_p0_1_second_confirm_while_first_in_flight_is_rejected` | — (safe) |
| 2 | `test_p0_1_concurrent_confirms_place_exactly_one_order` | xfail strict |
| 3 | `test_p0_2_kite_network_exception_reports_unknown` | — (safe) |
| 4 | `test_p0_2_entry_raw_network_error_strands_ticket[ReadTimeout]` | xfail strict |
| 5 | `test_p0_2_entry_raw_network_error_strands_ticket[ConnectionError]` | xfail strict |
| 6 | `test_p0_2_entry_accepted_but_response_lost` | xfail strict |
| 7 | `test_p0_2_exit_accepted_but_response_lost` | xfail strict |
| 8 | `test_p0_3_emergency_exit_in_dry_run_does_not_close_live_position` | xfail strict |
| 9 | `test_p0_3_exit_ticket_confirmed_in_dry_run_does_not_close_live_position` | xfail strict |
| 10 | `test_p0_3_breakeven_in_dry_run_keeps_db_stop_equal_to_broker_gtt` | xfail strict |
| 11 | `test_p0_4a_draft_ticket_cannot_be_confirmed[risk_check_crashed]` | xfail strict |
| 12 | `test_p0_4a_draft_ticket_cannot_be_confirmed[inserted_draft]` | xfail strict |
| 13 | `test_p0_4b_stale_exit_cannot_sell_already_closed_position` | xfail strict |
| 14 | `test_p0_4c_pending_entries_cannot_jointly_exceed_open_position_limit` | xfail strict |
| 15 | `test_p0_5_oco_stop_or_target_fill_closes_and_journals_position[stop]` | xfail strict |
| 16 | `test_p0_5_oco_stop_or_target_fill_closes_and_journals_position[target]` | xfail strict |
| 17 | `test_p0_6_triggered_gtt_with_complete_fill_closes_at_fill_price` | — (safe) |
| 18 | `test_p0_6_triggered_gtt_without_fill_keeps_position_open[unfilled]` | xfail strict |
| 19 | `test_p0_6_triggered_gtt_without_fill_keeps_position_open[partial]` | xfail strict |
| 20 | `test_p0_7_unfilled_exit_keeps_protection_and_position_open[limit_market_hours]` | xfail strict |
| 21 | `test_p0_7_unfilled_exit_keeps_protection_and_position_open[market_amo_after_hours]` | xfail strict |
| 22 | `test_p0_8_cross_site_post_is_rejected[confirm]` | xfail strict |
| 23 | `test_p0_8_cross_site_post_is_rejected[gtt_cancel]` | xfail strict |
| 24 | `test_p0_8_json_endpoints_reject_text_plain_body` | — (safe) |

## 3. Mapping: test → P0 finding → protected property

| Finding | Safety property protected | Tests |
|---|---|---|
| **P0-1** Non-atomic confirm | One approval produces at most one broker order. | 1, 2 |
| **P0-2** Raw network error strands the ticket | An ambiguous broker outcome is reported as UNKNOWN, and the ticket is left in a state something will follow up. | 3, 4, 5, 6, 7 |
| **P0-3** Global mode applied to live positions | A live position is only closed or adjusted by a real broker action. Dry run governs new entries only. | 8, 9, 10 |
| **P0-4** Confirm does not re-validate | Only a checked ticket executes; an exit only sells a still-open position; an entry is only placed if the portfolio still has room. | 11, 12 (A), 13 (B), 14 (C) |
| **P0-5** OCO positions never closed | A GTT stop or target fill closes and journals the position, trailing or not. | 15, 16 |
| **P0-6** "GTT triggered" treated as closed | Only a real fill, or the broker no longer holding the shares, closes a position. | 17, 18, 19 |
| **P0-7** Exit placement treated as exit fill | Until the exit fills, the position stays open, keeps its GTT, and is not journaled. | 20, 21 |
| **P0-8** CSRF | A cross-origin request cannot approve a trade or remove protection. | 22, 23, 24 |

## 4. Tests that currently XFAIL

There are 20 xfail cases: tests 2, 4–16 and 18–23 in the table above. All are `strict=True, raises=AssertionError`.

The four unmarked tests pass: 1, 3, 17 and 24.

## 5. Exact behaviour demonstrated by each XFAIL

Captured by running `pytest tests/test_live_safety.py --runxfail`, which executes xfail tests as normal tests. The ticket, order and GTT ids are the FakeKite's deterministic ids.

**P0-1: `test_p0_1_concurrent_confirms_place_exactly_one_order`**
- **Setup:** two threads confirm the same `pending_confirmation` ticket. Both are held at a `threading.Barrier` placed on `execution.get_execution_mode()`, which runs after the status check and before the `UPDATE` that claims the ticket.
- **Result:** both responses were **200**.
  - **2 BUY orders** and **2 protective GTTs** were placed.
  - The ticket tracks order `250000000002`. Order `250000000001` is live at the broker and **untracked** by the app.

**P0-2**

*`…entry_raw_network_error_strands_ticket[ReadTimeout]` and `[ConnectionError]`*
- **Setup:** `place_order` raises a raw `requests` exception before reaching the broker.
- **Result:**
  - The confirm returns **HTTP 500**.
  - The ticket is left at `status='confirmed'`, `kite_order_id=None`, `entry_fill_status=None`.
  - One run of the trailing, order-monitor and reconciliation cycles does not touch it.

*`…entry_accepted_but_response_lost`*
- **Setup:** the broker **accepted** the BUY, then the response raised `ReadTimeout`.
- **Result:**
  - HTTP 500; the ticket is stranded as above.
  - The broker holds order `250000000001`, which the app does not track.
  - **No protective GTT** was placed.

*`…exit_accepted_but_response_lost`*
- **Setup:** the broker accepted the exit SELL, then the response raised `ReadTimeout`.
- **Result:**
  - HTTP 500; the exit ticket is stranded in `confirmed`.
  - The original position still shows open (`closed_at=None`).
  - Its protective GTT `9001` is still **active**, although the shares were sold. That GTT is now an orphan that can later fire a SELL.

**P0-3**

*`…emergency_exit_in_dry_run…` and `…exit_ticket_confirmed_in_dry_run…`*
- **Setup:** a live, filled position; global mode switched to `dry_run`; the exit is attempted (EXIT ALL, or a single strategy-exit confirm).
- **Result:**
  - The exit ticket was created as `mode='dry_run'` and `status='executed'`.
  - The **live** position was stamped `closed_at`.
  - **No SELL reached the broker.** GTT `9001` is still `active`, and the broker still holds 5 shares.
  - Journal rows: 0. `record_closed_trade` raised its mode-mismatch `ValueError`, which was logged and swallowed.

*`…breakeven_in_dry_run…`*
- **Setup:** a live position; a breakeven proposal confirmed while global mode is `dry_run`.
- **Result:**
  - The app shows the stop at **3000.05**.
  - The broker GTT still triggers at **2900.0**; `modify_gtt` was called 0 times.

**P0-4**

*`…draft_ticket_cannot_be_confirmed[risk_check_crashed]`*
- **Setup:** `holdings()` raises a raw `ReadTimeout` during ticket creation. This is not a `KiteException`, so it escapes the risk engine.
- **Result:**
  - `POST /api/tickets` returns 500 and leaves the ticket as `draft` with `risk_check_results=NULL`.
  - Confirming it in live mode returned **200**, the ticket went to `executed`, and **1 real order** was placed without any risk check.

*`[inserted_draft]`*
- **Result:** identical to `[risk_check_crashed]`, for a `draft` ticket inserted directly.

*`…stale_exit_cannot_sell_already_closed_position`*
- **Setup:** a strategy exit is pending. EXIT ALL exits the same position, since Emergency Exit ignores the pending exit. The fill is observed, and the broker holds 0 shares.
- **Result:** confirming the stale exit returned **200** and `executed`, and sent **SELL #2**.

*`…pending_entries_cannot_jointly_exceed_open_position_limit`*
- **Setup:** `max_open_positions=1`. TCS and INFY each passed their checks alone. After TCS executed, re-running `risk_engine.run_checks` on INFY fails `max_open_positions`.
- **Result:** confirming INFY still returned **200** and `executed`. That makes **2 BUY orders** against a limit of 1.

**P0-5: `…oco_stop_or_target_fill_closes_and_journals_position[stop]` and `[target]`**
- **Setup:** a non-trailing live entry through the real path. Its OCO GTT fires, and the SELL fills COMPLETE: @2885.5 for the stop leg, @3300.0 for the target leg. The broker then holds 0.
- **Result:**
  - After one run of every background cycle, the position is **still open**.
  - **No `closed_trades` row** was written.

**P0-6: `…triggered_gtt_without_fill_keeps_position_open[unfilled]` and `[partial]`**
- **Setup:** a trailing live position. The GTT fires, and its SELL LIMIT sits **OPEN** with 0/5 filled (or 2/5 for `[partial]`). The broker still holds 5 (or 3).
- **Result:** the trailing engine **marked the position closed** and did all of the following:
  - synthesised exit ticket `{id: 2, entry_fill_status: 'estimated', entry_price: 3000.0}`, priced at the current LTP;
  - wrote a `closed_trades` row with `exit_reason: 'stop_loss'`.

**P0-7: `…unfilled_exit_keeps_protection_and_position_open[limit_market_hours]` and `[market_amo_after_hours]`**
- **Setup:** a live position held at the broker, with an exit order accepted but unfilled. It is `OPEN` for the LIMIT exit in market hours, and `AMO REQ RECEIVED` for the MARKET exit after hours.
- **Result:**
  - Protective GTT `9001` is **deleted**.
  - The position has `closed_at` set before any fill.
  - A journal row was written at the reference price: `exit_price=3100.0`, `pnl=500.0`.
  - 5 shares are still held throughout.

**P0-8: `…cross_site_post_is_rejected[confirm]` and `[gtt_cancel]`**
- **Setup:** a POST with `Origin: https://evil.example`, `Content-Type: text/plain`, no body and no application header.
- **Result:**
  - `/api/tickets/1/confirm` returned **200** and called `place_order` in live mode.
  - `/api/gtts/9001/cancel` returned **200** and called `delete_gtt`.

### Already-safe behaviour confirmed (passing tests)

| Test | What it confirms |
|---|---|
| `test_p0_1_second_confirm_while_first_in_flight_is_rejected` | A confirm that arrives while the first is **inside** `place_order` gets **409**, and only 1 order is placed. The first confirm commits `confirmed` before it calls the broker. |
| `test_p0_2_kite_network_exception_reports_unknown` | kiteconnect's own `NetworkException` is handled: HTTP 200, `success=False`, an "UNKNOWN" message, and the ticket is not stranded (audit S5). |
| `test_p0_6_triggered_gtt_with_complete_fill_closes_at_fill_price` | A triggered GTT with a matching COMPLETE fill closes the position, and the journal uses the real fill price. This must survive the P0-6 fix. |
| `test_p0_8_json_endpoints_reject_text_plain_body` | A `text/plain` body sent to the JSON endpoint `emergency-exit-confirm` returns **422**, and no SELL is placed. This supports the audit's statement that JSON-body endpoints are effectively protected. |

## 6. FakeKite capabilities added

All additions are generic and opt-in. When unused, FakeKite behaves exactly as before.

| Capability | Purpose |
|---|---|
| `raise_on_next(name, exc)` | Named alias for the existing `fail_next`. The call is recorded but has no effect, modelling a request that never reached the broker. |
| `accept_then_raise(name, exc)` | Backed by `fail_after_next`. The call takes full effect at the broker, then raises, modelling "accepted, response lost". Wired into `place_order`, `cancel_order`, `place_gtt`, `modify_gtt` and `delete_gtt`. |
| `on_call(name, fn)` / `before_call` | A hook run inside a broker call, before it takes effect, used to hold a thread there deterministically with a `threading.Event`. `fn=None` removes it. |
| `trigger_gtt(gtt_id, leg=0, status="COMPLETE", filled_quantity=None, average_price=None)` | Does what Kite does when a GTT fires: marks it `triggered` and creates the leg's LIMIT order. It can produce an unfilled (`OPEN`), partial or full fill. |
| `set_holding(symbol, quantity, t1_quantity=0, …)` | Controls broker holdings, including T+1 quantity. Quantities of 0 remove the holding. |
| `set_position(symbol, quantity, product="CNC")` | Controls broker net positions. |
| `_new_order(...)` | A private helper factored out of `place_order` and shared with `trigger_gtt`. The order dict shape is unchanged. |

Existing mutation helpers are reused as they were: `set_order_state` (order status and partial/full fill) and `set_gtt_status`.

Fills never change holdings automatically, because existing tests rely on static holdings. Tests set broker holdings explicitly.

## 7. Total pytest result

```
.venv/Scripts/python -m pytest
119 passed, 20 xfailed in ~22.5 s
```

- **Before PK-0:** 115 passed.
- **After PK-0:** the 115 previous tests plus the 4 new already-safe tests pass, and there are 20 strict xfails.
- **Failures and XPASSes:** none, in 3 consecutive full-suite runs.
- **Sleeps:** `tests/test_live_safety.py` contains none. The word appears only in the module docstring.
- **Concurrency:** handled with a `threading.Barrier` and `threading.Event`s. Their timeouts are only safety nets, not waits.

## 8. Audit findings that could not be reproduced deterministically

**None.** All eight P0 findings were reproduced deterministically. Two limitations of the reproductions:

- **P0-2** relies on FakeKite raising the raw `requests` exceptions directly. That this is what the real SDK does was confirmed by reading the installed `kiteconnect` 5.0.1: `connect.py:_request` wraps `reqsession.request(...)` in `except Exception as e: raise e`, with a default timeout of 7 s.
- **P0-8** is reproduced at the HTTP layer. The test sends the headers a cross-site simple request would carry. It does not drive a real browser, so CORS preflight behaviour is inferred rather than exercised. The server-side acceptance is what matters, and it is proven.

The timing-dependent P1-8 race (trailing closure versus explicit exit) is outside PK-0's scope and was not attempted.

## 9. Audit findings that turned out to be incorrect or overstated

**P0-1 (overstated window; the finding itself is correct).**
- **What the audit says:** the race is open "within the ~1 s a Kite round trip takes", and "the same thing happens with a client retry after a slow response".
- **What the code does:** `confirm_and_execute_ticket` commits `status='confirmed'` before it calls the broker (`routes/tickets.py` L445–447).
  - A second confirm that arrives while the first is waiting on Kite is therefore already refused with 409, and so is a slow-response retry. `test_p0_1_second_confirm_while_first_in_flight_is_rejected` proves this.
  - The real window is the local read → check → `UPDATE` → commit gap (L437–447): milliseconds of SQLite work, not a broker round trip.
- **Conclusion:** the double-order defect is real and reproduced. Two requests that pass the status check before either commits both place orders. It is narrower than the audit describes, but P0-1 still stands, and the recommended conditional `UPDATE … WHERE status='pending_confirmation'` is still the right fix.

**Everything else matched.** Every other P0 detail checked matched the code and the observed behaviour:
- the mode-mismatch `ValueError` being swallowed (P0-3);
- the LTP-priced `'estimated'` synthetic exit (P0-6);
- the reference-price journal row and GTT deletion on placement (P0-7);
- the 500 from ticket creation leaving a `draft` (P0-4A);
- the JSON-body endpoints being safe (P0-8).

---

## Notes for the fix packets

These pre-existing tests encode today's defective behaviour and will need updating when the corresponding fix lands. The audit already flags the first two.

| Test | Current assertion | Conflicts with |
|---|---|---|
| `test_review_changes.py::test_trailing_triggered_gtt_is_detected_as_closed` | A `triggered` GTT with **no fill at all** closes the position | P0-6 fix |
| `test_baseline_api.py::test_trailing_detects_vanished_gtt_and_records_close` | A GTT missing from `get_gtts()` closes the position | P0-6 fix |
| `test_review_changes.py::test_order_monitor_rejected_exit_reopens_position` | `closed_at` is set immediately after an unfilled exit is confirmed | P0-7 fix |
| `test_baseline_api.py::test_exit_ticket_closes_position_and_records_trade`, `test_emergency_exit` | `closed_at` is set right after an exit confirm (auto-filled, but before `order_monitor` runs) | P0-7 fix. Run an order-monitor cycle before asserting. |
| Every `env.client.post/put/delete` in the suite | Sends no application CSRF header or matching `Origin` | P0-8 fix. The simplest route is to set default headers on the `TestClient` in `conftest.env`. |

When a fix makes a strict xfail in `test_live_safety.py` XPASS, remove its `@_xfail(...)` marker. It then becomes a normal regression test.
