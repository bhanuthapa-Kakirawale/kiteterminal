# Phase 3 Stage S4: implementation report (protection engine foundation and operation state machine)

**Date:** 2026-11-11 · **Branch:** `feature/signal-detail-and-strategy-revision` · **HEAD:** `35a0e07f…` (unchanged;
nothing staged, committed, pushed or stashed) · **Specification:** `docs/PHASE_3_DESIGN_DECISIONS.md` (R6)

**Result: S4 accepted** against the criteria in §6. Dark only: migration v12 is **not** registered, nothing is
wired into a live path, `LIVE_ENTRIES_ENABLED` stays `False`, and every database used was a pytest temporary
database.

## 1. Files

| File | Status | Purpose |
|---|---|---|
| `backend/protection.py` | new | The journal: §4.2 transition table, fenced CAS, DD-5 claims, DD-7 events, DX classifier, derived protection state, Q3/R2-6 manual resolution, Q1 supersede overlay, DD-6 link guard |
| `backend/protection_evidence.py` | new | One broker read as `Evidence` (normalised GTT/order views, `broker_reads` row, DD-8 freshness) |
| `backend/config.py` | modified (append only) | DD-8 placeholders, each marked PENDING Phase 9 |
| `tests/protection_helpers.py` | new | Dark journal on `env`'s tmp DB, positions, FakeKite GTTs, evidence, test-only `force_state` |
| `tests/test_protection_s4.py` | new | 157 tests (§4) |

## 2. What was implemented, against R6

| R6 item | Implementation | Test |
|---|---|---|
| §4.2 table; nothing leaves a terminal state | `ALLOWED_TRANSITIONS` (15 pairs, each tagged with its rows) | T01 (all 110 ordered pairs), T02 |
| DD-5 one CAS per change; rowcount 0 = refused | `transition()`: `UPDATE … WHERE id AND op_uuid AND state IN (…) [AND attempts]` | T01, T03, T04 |
| DD-5 claim by `INSERT … SELECT`; R2-4 NULL mode | `claim()`: account_id/mode copied from `trade_tickets`; `t.mode IS NOT NULL` | T05, T06 |
| Every journal write inside `fenced_transaction` | `journal_transaction()` + a per-thread marker checked by every write | T21, T22 |
| Partition indexes (one open change; one open cancel per target) | DDL (S1); refusals surface as `IntegrityError`, the transaction rolls back | T07, T08, T09 |
| Row 1 claim predicates (open cancel, exit in flight, open superseded op, DD-9, DX for automatic claims, re-claim bound) | `claim()` | T07, T16, T17 |
| M-5 conditions 1–4 for a create | `create_claim_problem()` | T12–T15 |
| DD-6 engine link guard, independent of the unique index | `link_gtt()` (`NOT EXISTS` another ticket with the id) | T19, T20 |
| DD-7 exactly one event per transition; 13 types | `transition()` / `claim()` / `supersede_open_changes()` | T01, T37 |
| DX rev. 3 precedence (R3-1), effect class never a column | `classify()`; the record goes into `evidence` with the read id | T24–T26 |
| DD-4 direction by riskiest component | `direction_of()` | T27 |
| DD-7 derived `CLOSED_CLEANUP_REQUIRED` (never stored) | `derive_protection()['cleanup_required']` | T29 |
| P:256 `PROTECTION_CONFIRMED` only on a fresh complete read with the full shape; never auto-unlink | `derive_protection()` | T28 |
| Q3 resolution: only from `needs_review`; fresh complete `resolution` read; outcome/id shape; one per op | `resolve_manual()` | T30 |
| R2-6 resulting protection state; never CONFIRMED by outcome; flat + linked → automatic `position_closed` cancel | `resolve_manual()` | T31–T33 |
| Q1 overlay: intent → not_sent; others keep their state; open cancels never superseded | `supersede_open_changes()` | T34 |
| DD-2 v12 stays unregistered | (no change to `MIGRATIONS`) | T23 |
| DD-5/DD-3 static single-writer and literal checks | — | T35, T36 |

## 3. Design notes

- **Operation state vs superseded status.** The overlay columns are written only by `supersede_open_changes()`,
  never by `transition()`. A superseded op keeps its state (row 19) except `intent`, which becomes `not_sent` in the
  same UPDATE (row 4), because the CHECK forbids the overlay on `intent`.
- **Unresolved ops stay represented.** No code path deletes a journal row or moves an open op to a terminal state
  without a §4.2 transition; terminal ops have no outgoing transition.
- **`attribution_cap` is never `caps_sell_exposure` yet.** R2-1 condition 3 needs a baseline, which does not exist
  before Phase 7 (`startup_reconciliation.evaluate_attribution` says so). The classifier therefore yields
  `reduces_protection` for every cap until a caller can prove all seven conditions (fail-closed).
- **Defence in depth.** Mutation M2 (table not consulted) is caught both by T01 and by the DDL CHECKs: an illegal
  transition that slipped past the table would still violate a CHECK in most cases.

## 4. Tests

`tests/test_protection_s4.py`: **157 passed, 0 failed** (`.venv` pytest, sequential; the parametrised T01 is 110 of
them). Guard summary: `guard refusals: 0 (unexpected 0); network refusals: 0`.

## 5. Mutation checks (scratchpad `mutate.py`, exact-once replacement, sha256 restore verified)

| # | Mutation | Result |
|---|---|---|
| M1 | CAS ignores the from-state | caught (T03) |
| M2 | transition table not consulted | caught (T01) |
| M3 | DX evaluates `none` before the cap step | caught (T24) |
| M4 | CAS drops the attempt key | caught (T04) |
| M5 | `resolved_manual` maps to CONFIRMED | caught (T31) |
| M6 | NULL-mode ticket journaled | caught (T06) |
| M7 | journal write without the fence marker | caught (T21) |
| M8 | M-5 condition 4 dropped | caught (T13) |
| M9 | DD-9 not required | caught (T16) |
| M10 | automatic cancel classified by kind | caught (T17) |
| M11 | DD-6 link guard dropped | caught (T20) |
| M12 | CONFIRMED without a fresh read | caught (T28) |
| M13 | an outgoing transition from a terminal state | caught (T01) |

## 6. Acceptance

| Criterion | Status |
|---|---|
| Every legal transition succeeds and every illegal one is refused with nothing written | met (T01) |
| Duplicate and concurrent claims produce at most one op | met (T07, T09) |
| Rollback on failure; partial writes impossible | met (T10, T11) |
| Missing broker evidence never yields a create or a CONFIRMED state | met (T12, T28) |
| Manual resolution and needs-review per Q3/R2-6 | met (T08, T30–T33) |
| Existing dark-schema compatibility; v12 unregistered | met (T22, T23; dark schema applied by the existing S1/S2 function) |
| All mutations caught | met (13/13) |

## 7. Limitations (carried forward)

- Broker behaviour is modelled by FakeKite only. Nothing here establishes Kite behaviour.
- The DD-8 values are placeholders (Phase 9).
- `derive_protection` needs the intended shape from the journal; a position linked by legacy code without a journal
  op derives `PROTECTION_UNKNOWN` (`INTENDED_SHAPE_UNKNOWN`), never CONFIRMED.
