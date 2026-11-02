# SEC-1 P1-E: broker acceptance versus actual fills

**Scope.** P1-E of the cross-repo audit remediation plan. FakeKite and
temporary databases only. No frozen PK-25 file was touched.

## Finding closed

A ticket's stored status `executed` has meant "the broker accepted the
order" since PK-8. It never meant "the order filled". The fill was tracked
separately (`entry_fill_status`, `filled_quantity`, PK-8/PK-13), but the API
had no explicit field saying "accepted" versus "filled". A client reading
`status == "executed"` could take an acknowledgement for a fill.

## Design (owner decision: derived API fields)

The stored `status` keeps its meaning and its history.

- **No migration.** No historical record is rewritten.
- **No frozen file is touched.** `execution.py` writes the status and stays
  byte-identical.
- **Nothing that reads `executed` changes behaviour.** There are 88
  occurrences across 16 backend/frontend files, among them the order monitor,
  reconciliation, the journal, position quantities, risk, the kill switch,
  trailing, live readiness and the lifecycle.

Instead, a pure `lifecycle.order_progress(row)` states the two facts
separately, derived only from what is persisted:

| Persisted state | `acceptance` | `fill` |
|---|---|---|
| draft / awaiting approval / risk-rejected, or refused / withdrawn before any broker call | `NOT_SUBMITTED` | `NONE` |
| claimed, broker call in flight | `SUBMITTING` | `UNKNOWN` |
| outcome unknown (or a stale claim) | `UNKNOWN` | `UNKNOWN` (quantity `null`) |
| contradictory broker evidence | `NEEDS_REVIEW` | `UNKNOWN` |
| proven never to have reached the broker | `NOT_FOUND` | `NONE` |
| definitively rejected at submission | `REJECTED` | `NONE` |
| accepted, working, nothing filled | `ACCEPTED` | `AWAITING_FILL` (`working: true`) |
| accepted, working, part filled | `ACCEPTED` | `PARTIAL` (`working: true`) |
| accepted, ended after a partial fill | `ACCEPTED` | `PARTIAL` |
| accepted, fully filled | `ACCEPTED` | `FILLED` |
| accepted, then cancelled / exchange-rejected, nothing filled | `ACCEPTED` | `CANCELLED` / `REJECTED` |
| left the book before its fill was confirmed | `ACCEPTED` | `UNCONFIRMED` |
| legacy row: no submission record / no fill record | `NOT_RECORDED` | `NOT_RECORDED` (quantity `null`) |

Rules the mapping follows:
- `filled_quantity` comes only from what the order monitor recorded from the
  broker's order history (`fill_evidence: "BROKER"`).
- A pre-fill-tracking row whose quantity came from the migration backfill
  says so (`fill_evidence: "LEGACY_RECORD"`). It is never presented as
  broker-verified.
- An acknowledgement is never a fill.
- An unknown submission's fill is unknown, never assumed.
- Paper (dry-run) orders carry `simulated: true`.

## Changes

- `backend/lifecycle.py`: `order_progress()` and its documented vocabulary.
- `backend/routes/tickets.py`: every ticket payload carries `order_progress`.
- `backend/lifecycle.derive` (and so `GET /api/app-positions`): every
  position carries `entry_progress`.
- `frontend/app.js`: the ticket detail shows "Broker / fill: accepted ·
  partly filled · 40/100 · order working" ("Paper broker" for dry run),
  worded from the server's values. Nothing is shown before the order is
  sent. The existing badges and toasts already said "placed" or "awaiting
  fill", never "executed".

**No behaviour change.** No resubmission, and no change to the order monitor,
reconciliation, partial-fill accounting or order-tag matching.

## Tests

`tests/test_order_progress.py` (22), FakeKite with the real order monitor:
- an acknowledgement with zero fills;
- a partial fill (working);
- a full fill;
- rejection after acceptance;
- cancellation (with nothing filled, and after a partial fill);
- rejection at submission;
- a ticket not submitted;
- an unknown submission, then adopted by its tag (never re-placed);
- an unknown submission that never reached the broker, then `NOT_FOUND` after
  the window;
- delayed broker history (5 polls stay `AWAITING_FILL`);
- an unreachable history never becomes a fill;
- repeated polling after the fill changes nothing (progress and events);
- a duplicate approval: 409, no resubmission;
- an out-of-order `OPEN` after `COMPLETE` never regresses the recorded fill;
- a restarted process resumes from the persisted state;
- an exit order's own progress;
- the dry-run `simulated` flag;
- the positions payload's `entry_progress`;
- legacy rows are never "broker-verified";
- the mapping is total over every persisted combination and never infers
  `FILLED` from an acknowledgement;
- every value has a UI label (Node renderer).

Every test asserts the `place_order` count, so nothing is resubmitted.

## Not changed / limitations

- The stored `status` vocabulary is unchanged. Renaming it would touch the
  frozen `execution.py` and every consumer, and would need separate approval.
- The positions list keeps its lifecycle badges. `entry_progress` is in the
  payload, but is not yet rendered there; the ticket detail shows it.
