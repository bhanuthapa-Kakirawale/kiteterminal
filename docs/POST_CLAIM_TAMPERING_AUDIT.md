# Post-claim tampering auditability

**Scope.** This closes the known P1-D limitation: a change to a ticket's stored order details *after* it was claimed for execution was not recorded. The broker received the verified values, and the tampered row was left as written, without any audit event.

**Tested with:** FakeKite and throwaway databases. No frozen PK-25 file was touched: `backend/execution.py` is byte-identical.

## 1. What is bound to a claimed ticket

The economic and identity fields are exactly those of P1-D's review digest (`account_safety.ticket_review_digest`). They are grouped into categories so that an audit event can say *what kind* of thing changed without recording any value:

| Category | Fields |
|---|---|
| `identity` | id, account_id, source, closes_ticket_id, symbol, exchange, instrument_token |
| `side` | transaction_type |
| `quantity` | quantity |
| `prices` | entry_price, stop_loss, target_1, target_2 |
| `order_type` | entry_order_type, product_type |
| `protection` | use_gtt, trailing_enabled, trailing_distance, trailing_trigger |

`test_the_categories_cover_exactly_the_p1d_digest_fields` keeps the categories and the digest identical.

## 2. Lifecycle (`backend/ticket_integrity.py`, migration v10)

1. **Claim.** In the same transaction as the `pending_confirmation → confirmed` claim, a `CLAIM_SNAPSHOT` is appended. It holds:
   - the overall digest of the row the confirmation was decided on;
   - one SHA-256 per category.

   For a reviewed manual or Quick-Action BUY, the snapshot digest equals its `review_digest`. Strategy entries and exits are snapshotted too; before this change they had no content binding at all.

2. **Validation points.** At each point, the persisted row is compared with the snapshot:

| Stage | When | On a change |
|---|---|---|
| `post_claim` | the row re-read right after the claim | event recorded; claim withdrawn; ticket **rejected**; no broker call |
| `pre_broker` | re-read after revalidation and the final gate, immediately before the broker call | the same |
| `post_execution` | after the broker call | event recorded; the response carries `integrity_warning`. The broker had received the **verified in-memory row**. |
| `trailing` | before the trailing engine rebuilds a GTT from the persisted row | event recorded; the GTT is **not** modified; the existing protective GTT stays |

3. **A `TAMPER_DETECTED` event** records:
   - `ticket_id` and `account_id`;
   - `stage`;
   - the changed `categories`;
   - `claimed_digest` and `current_digest`;
   - `detected_at` (UTC).

   It records only digests and category names. It never records values, prices, quantities, credentials or account details (`test_events_hold_digests_and_category_names_only`).

4. **Permanence.** P1-D's permanent rejection is preserved and extended:
   - Once a ticket has a `TAMPER_DETECTED` event it can never be confirmed or reverted (`TICKET_TAMPERED_AFTER_CLAIM`).
   - Even if someone pushes it back to `pending_confirmation` in SQL, it is rejected again.
   - No fresh review token is ever issued for it: re-review covers pending tickets only, and never re-stamps the digest.

5. **Repeated detection.** The same detection (same stage, same current content) is recorded once. A different stage is recorded again. The trailing engine therefore does not flood the log every few seconds.

6. **The audit store** (`ticket_integrity_events`):
   - **Append-only.** `BEFORE UPDATE` and `BEFORE DELETE` triggers abort.
   - **Hash-chained.** `entry_hash = sha256(prev_hash + every field)`.
   - `verify_chain()` detects an edited, deleted or reordered entry.
   - `GET /api/tickets/{id}/integrity` returns the ticket's events and the chain verification. It is read-only.

## 3. Behaviour change versus P1-D (deliberate)

| | Before | Now |
|---|---|---|
| A write during confirm-time revalidation (after the claim) | The verified row was sent; the tampered row was kept, **unrecorded** | **Refused before the broker call**, recorded, ticket rejected |
| The same for a strategy entry (no review digest) | Not detected | Detected and refused |

One existing test was changed on purpose: `test_tampering_after_the_claim_cannot_change_what_is_sent` became `test_tampering_after_the_claim_never_reaches_the_broker`. It still asserts that no tampered value is sent; now nothing is sent at all.

## 4. Boundary of the solution

This is **application-level** detection. It proves that the row the broker received was the one confirmed. It records any change made afterwards by anything that writes SQL but does not rewrite this log.

It is **not** proof against someone with control of the database file. Such a person can:

- drop the triggers;
- rewrite trade rows and the whole chain consistently;
- or delete the file.

`test_boundary_a_full_consistent_rewrite_of_the_file_is_not_detectable` pins that limitation, so it is never claimed away.

A stronger guarantee would need an external anchor, such as periodically exporting the latest `entry_hash` off the machine. That is not part of this sprint.

Other limitations:

- **Exits are claimed through the same confirm path**, Emergency Exit included. An exit ticket whose *own* row changes between its claim and the broker call is therefore refused rather than sent unverified. That window has no broker call in it, since exits skip revalidation. A tampered *parent* position does **not** block its exit, in line with P1-D's rule that exits are never held back for risk reasons. The exit quantity comes from broker-recorded fills (`position_quantity`), not from the parent's `quantity` field.
- **Tamper detection runs only at the points listed in §2.** Read-only screens show the stored row as it is. The integrity endpoint and the ticket's events say whether it was tampered.

## 5. Tests

`tests/test_ticket_integrity.py` has 14 tests:

- **legitimate tickets:** manual, strategy and dry run;
- **post-claim changes:** at the `post_claim`, `pre_broker` (strategy entry), `post_execution` and `trailing` stages;
- **repeated detection;**
- **permanence:** confirm, revert and re-review are all refused;
- **append-only triggers;**
- **chain detection:** an edited entry and a deleted entry are both caught;
- **the full-rewrite boundary;**
- **no values in events;**
- **the read route.**

The acceptance test (stage 16) shows the same refusal over HTTP against a real server process.

## 6. Operator impact

- **Migration v10.** The next start of the real terminal migrates the database to v10. It takes the automatic `.pre_schema_v10_bak` backup and adds one table. No trading table changes.
- **If you see `TICKET_TAMPERED_AFTER_CLAIM`:**
  1. Nothing was sent.
  2. Create a new ticket.
  3. Investigate what wrote to the database.
