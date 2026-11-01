# SEC-1 P1-D: manual-ticket re-review and stale-signal safeguards

**Scope.** P1-D of the cross-repo audit remediation plan. P0-A to P0-C are in
`docs/SEC1_LIVE_SAFETY_REPORT.md`.
- **Not started:** P1-E, P1-F, P2-G and the DMB candidate inbox.
- **Never done:** no real broker order was placed or tested. Everything runs
  against FakeKite and temporary databases.

## Finding closed

PK-12E refuses a strategy entry once its signal candle is stale. Manual and
Quick Action buy tickets were explicitly left out ("an old manual ticket is
still approvable", PK-12 report, PK-12E). As a result, a manual BUY created days
earlier could still be approved and sent:
- under an earlier Kite login;
- with stored levels changed outside the app.

The confirm request also never said which version of the ticket the approver
had seen.

## The rule

A **review subject** is a risk-adding manual or Quick Action BUY entry
(`source` `manual` / `alert_action`, not a closing sell). Strategy entries are
judged by their candle. Exits, Emergency Exit, Quick Action sells, GTT and
trailing upkeep, cancels and switching to dry run are never held back.

A subject may be sent only if every one of these holds:

| Condition | Failure | Ticket afterwards |
|---|---|---|
| A review record exists (creation, edit or Re-review) | `TICKET_REVIEW_REQUIRED` 409 | pending |
| Its time is readable and at most 120 s in the future | `TICKET_REVIEW_REQUIRED` 409 | pending |
| It happened under the **active Kite login** (`sessions.id`) | `TICKET_REVIEW_REQUIRED` 409 | pending |
| It is in the **same NSE session window** as now | `TICKET_REVIEW_REQUIRED` 409 | pending |
| The stored identity and economic fields still hash to the reviewed digest | `TICKET_EVIDENCE_MISMATCH` 409 | **rejected** (permanent) |
| The confirm echoes the review token its view showed | `TICKET_REVIEW_CHANGED` 409 | pending, nothing written |
| The review can be evaluated at all | `ACCOUNT_SAFETY_UNAVAILABLE` 503 | pending |

**Session window** (`account_safety.review_window`):
- `("OPEN", day)` during a regular session;
- otherwise `("CLOSED", the session an order would wait for)`.

It is read from the trading calendar (`trading_calendar.is_market_session_open`,
holiday-aware), never from `market.is_market_open`. A review is current only
inside one window:
- Monday 10:00 → 15:29 is current; at 15:30 it is not.
- Friday 20:00 → Monday 09:14 is current, so a weekend-prepared AMO still
  works. At Monday 09:15 it is not.

The rule applies in both modes: it is a property of the human review, not of
money.

**Digest and token** (`account_safety.ticket_review_digest`, `review_token`):
- The digest is sha256 over `id`, `account_id`, `source`, `closes_ticket_id`,
  the instrument, side, quantity, prices, order type, trailing and product
  fields. It is always computed from the **stored** row, so two identical
  tickets never share it.
- The token is sha256 of `id | digest | reviewed_at | reviewed_session_id`. It
  changes with every review, so a dialog left open across another tab's
  Re-review is refused.

## Changes

- **Migration v7 `p1d_ticket_review`** (`backend/db.py`):
  `trade_tickets.reviewed_at`, `reviewed_session_id` and `review_digest`.
  It is append-only, and every `ALTER` is guarded by `_column_exists`. Existing
  rows stay NULL ("not reviewed").
- **`backend/account_safety.py`:** `check_ticket_review`, called in two places:
  - `precheck`, after the stale-signal check, using the same `now` as the order
    variety;
  - `final_gate`, on the row read after the claim, in both modes, without the
    token, before the single LTP call.
  It is never part of the atomic claim: a lost claim with an entry-limit
  predicate is reported as `DAILY_ENTRY_LIMIT`.
- **`backend/routes/tickets.py`:**
  - Creation and edit stamp the review from the stored row, in the same
    transaction. Revert does not stamp.
  - `POST /api/tickets/{id}/re-review` covers pending review subjects only
    (`TICKET_REVIEW_NOT_APPLICABLE` otherwise):
    - it re-runs the risk checks against the current portfolio and records the
      time and session (compare-and-set);
    - it never re-stamps the digest, so tampered content is rejected. The only
      exception is a legacy row's first digest;
    - if the checks cannot run, it returns 503 and leaves the ticket unchanged;
    - a failed check moves the ticket to `risk_rejected`;
    - it is allowed while trading is locked.
  - Every response an approval view opens from carries a `review` object:
    `{subject, current, code, reason, reviewed_at, token}`.
- **`backend/models.py`:** `TicketConfirmRequest.expected_review_token`.
- **Strategy hardening** (`check_stale_signal`):
  - A signal dated after today, or whose `entry_evaluation.signal_date`
    differs from the ticket's, is a permanent `STALE_SIGNAL`.
  - An unreadable evaluation record never rejects a ticket by itself.
- **Frontend** (`app.js`, `index.html`, `style.css`):
  - Each view echoes the token it captured: the confirm dialog at open,
    Approvals cards from their rendered ticket, Signal Detail at open.
  - The dialog shows the review state and offers **Re-review** instead of
    Approve when the review is not current.
  - Pending cards show a "needs re-review" chip.
  - Review codes anywhere in the failure list reopen the dialog.
- **Docs:** USER_GUIDE, FIRST_LIVE_TRADE, TESTING, DESIGN, DEPLOYMENT and the
  PK-12 report.

## PK-12H timing change

Since PK-12H, the order variety (regular / AMO) is decided once in precheck
and bound to the execution. For a review subject, the final gate now also
re-checks the review window. Suppose an NSE open or close falls between
precheck and the broker call, for example a regular order decided at 15:29:59
that reaches the final gate at 15:30:01. The claim is then withdrawn and the
ticket returns to `pending_confirmation` (`TICKET_REVIEW_REQUIRED`). Before P1-D
the order was sent and Kite rejected it.

Strategy entries keep the PK-12H behaviour unchanged.

## Deviations and decisions

- **The final gate does not re-read the ticket** (plan R6). A claimed ticket
  cannot be edited through the API, and the broker is sent exactly the row the
  final gate verified. A database write that lands after the claim, outside the
  API, therefore cannot change what is sent. The test
  `test_tampering_after_the_claim_cannot_change_what_is_sent` proves the order
  and its protective stop are the reviewed ones. The tampered row itself is
  left as written, for reconciliation to see.
- **Strategy entries carry no client token** (plan, unresolved item: proposed
  no). Their content is not editable, and `strategy_engine.py` is frozen, so it
  cannot stamp at creation.
- **Token resolution is one second** (`reviewed_at` uses the codebase's
  `YYYY-MM-DD HH:MM:SS`). Two re-reviews of the same ticket within one second
  produce the same token.

## Tests

- **Suite:** 2,029 passed, 0 failed, 0 skipped. The baseline was 1,940; the
  additions are:
  - 83 new tests in `tests/test_manual_ticket_review.py`;
  - 5 new auto-enumerated CSRF cases for the new route;
  - 1 new `test_12e` twin.
- **Mutation checks**, each caught by the new file:
  - review check removed from precheck (16 failures);
  - review check removed from the final gate (3);
  - window ignores the open and close (6);
  - token never compared (3);
  - re-review skips its evidence check and re-stamps (1).
- **Deliberately changed tests** (no gate weakened):
  - Schema pins 6 → 7 (`test_live_preflight.py`, `test_platform_pk22.py`),
    plus the v7 effect in the synthetic pre-PK-22 migration.
  - Confirm bodies of review subjects send the token through
    `conftest.approval_body(db, ticket_id)` (mechanical replacement of
    `displayed_mode(...)`). `test_mode_binding._confirm` reads it from the
    ticket payload, as the UI does.
  - `test_baseline_api._insert_ticket` stamps a just-made review on
    approvable manual / Quick Action BUY rows. This follows the
    `seed_healthy_live_state` precedent. Gate tests opt out with
    `reviewed_at=None`.
  - `test_12c_next_ist_day_resets_the_count` and
    `test_12f_next_trading_day_resets` re-review after moving the clock a day.
  - `test_12e_manual_ticket_semantics_are_preserved` is now
    `..._is_never_judged_as_a_strategy_signal`, plus an unreviewed twin that
    expects `TICKET_REVIEW_REQUIRED`.
  - The ticket list's SELECT bound is 6: one constant session read (still
    asserted not to grow with rows).
- **Frozen PK-25 files** are byte-identical (`test_26`, `test_36`).

## Not verified

- The stock `tests/e2e_ui.py` cannot get past its first approval on a weekend:
  the browser's own market clock says "closed", so the card opens the AMO
  dialog. HEAD's frontend fails at the same step, so this is pre-existing. A
  focused Playwright pass of the P1-D flows against the demo server passed.
- No real Kite session, order or browser other than headless Edge.

## Operator impact

- **The next app start migrates the database to v7**, taking the automatic
  `.pre_schema_v7_bak` backup. Every manual / Quick Action buy ticket pending
  at that moment needs one Re-review.
- A manual ticket can be approved only in the NSE session window and Kite login
  in which it was created, edited or re-reviewed. After the 09:15 open, the
  15:30 close or a new login, open it and press **Re-review**. Re-review
  re-runs the risk checks; check the levels against the market first.
- A ticket whose details were changed outside the app is rejected. Create a new
  one.
