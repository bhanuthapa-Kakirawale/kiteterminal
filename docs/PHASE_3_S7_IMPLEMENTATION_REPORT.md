# Phase 3 Stage S7: implementation report (Emergency Exit and cancellation coordination)

**Date:** 2026-11-11 · **Branch / HEAD:** unchanged · **Specification:** M-7 (R2-3, R3-2, R3-3, R4, R5-1, R5-2, R6),
Q1 / §4.3, M-1, M-2 · **Not wired** into `routes/kill_switch` (Phase 6, separate authorisation, live blocker)

**Result: S7 accepted.** One safety defect was found by the tests during this stage and fixed (§4).

## 1. Files

| File | Status | Purpose |
|---|---|---|
| `backend/emergency_exit_engine.py` | new | `emergency_exit()` engine API; `outstanding_sell_orders` (E3 part); write-ahead fenced SELL |
| `backend/protection_exposure.py` | modified (own S6 file) | removal by (a) of a GTT deleted after it triggered (§4) |
| `backend/protection_evidence.py` | modified (own file) | `Evidence.raw` for the attribution check |
| `tests/test_protection_s7.py` | new | 32 tests |
| `tests/test_protection_s6.py` | modified (own file) | X21 (§4) |

## 2. The exact sale gate (all must hold; otherwise nothing is sold)

1. preconditions: position open, quantity known, valid mode, no exit in flight, no EE already running on the
   instrument in this process (single-flight) — a refused position is not superseded and nothing is claimed;
2. one fenced transaction: exit-owner claim (the existing unique index stops a concurrent click in any process),
   supersede overlay, `exit_prepare` cancels for app-proven ids only (linked, acknowledged result, open modify target),
   none for an id with an open cancel;
3. each cancel sent once (S5 fenced send; never retried while the position is open);
4. grace from **T_last** (latest committed send of this click) to T_last + `PROTECTION_INFLIGHT_GRACE_SECONDS`, ended
   early only when every relevant cancel is `confirmed` or `rejected`; no cancel sent → no wait;
5. exactly one R_EE (`emergency_exit`), complete, DD-8 fresh, started after initiation and after T_last; relevant cancels
   reconciled against it;
6. 𝒢 established, **G_max = 0 and S_agg = 0** (S6), attribution `exclusive`/`shared`, and no outstanding SELL order
   on the instrument (E3).

Then the SELL: `submitting` committed first, the call inside a held fenced transaction, outcome recorded; an ambiguous
outcome is `unknown` and never re-sent; the POSSIBLE_DOUBLE_SELL watch is armed (URGENT finding). On refusal: the
exit ticket (never sent) is cancelled; the result lists every member, its sources and w, with the R6 instruction.
**No override, no deferred or background sale** (static test E25).

## 3. Test → R6 traceability

| Scenario (mandate §8) | Test | Grace row |
|---|---|---|
| fully safe exit | E01 (cancel, full grace, one R_EE, one SELL, watch armed) | G3 |
| no cancel → no wait | E02 | — |
| multiple GTTs / multi-leg | E03 | G3 |
| active GTT after the cancel | E04 | G14 |
| unknown / unlinked GTT, never cancelled | E05 | G20 |
| incomplete listing (R_EE unusable) | E06 | G22 |
| acknowledgement without deletion proof | E07 | G4 |
| late cancellation; re-click sends no new cancel and starts no new grace | E08 | G4 → G3 |
| triggered order outstanding; partial fill | E09 | G6 |
| expected but invisible triggered order | E10 | G6 (T6) |
| id-less unknown create | E11 | G21 |
| confirmed cancel contradicted by ACTIVE | E12 | G2 (URGENT, NEEDS_REVIEW, terminal op unchanged) |
| refused preconditions → no overlay, no claim | E13 | — |
| attribution not exclusive/shared | E14 | — |
| grace starts at the last send (6 s later send still gets the full grace) | E15 | R3-3 A/B |
| definite result ends the wait | E16 | — |
| concurrent clicks (4 threads) → one SELL | E17 | — |
| another process owns the exit | E18 | — |
| crash during cancel / after cancels / after R_EE / during the SELL | E19 (×4) | — |
| ownership takeover during the sequence | E20 | — |
| timeout after an accepted cancel (deleted listed / pruned) | E21 | G10 / G12 |
| timeout after an accepted SELL → never re-sent | E22 | — |
| §4.3 late results: open → link; flat → automatic cleanup | E23, E24 | — |
| no override / no deferred sale (static) | E25 | — |
| GTT deleted after trigger with an open order | E26, X21 | (e) item 4 |
| acknowledged cancel, GTT missing from the listing | E27 | G5 |
| foreign outstanding SELL order (E3) | E28 | — |

## 4. Defect found and fixed during S7

**Finding.** E09/E10 initially **failed with a SELL submitted**: the linked GTT had triggered (its SELL order OPEN);
EE's `exit_prepare` deleted the GTT (FakeKite marks it `deleted`, as Kite is assumed to); R_EE listed it `deleted`;
condition (a) removed the member; nothing else stood in the way. Deleting a GTT does not cancel an order it already
placed, so the engine would have sold against a still-working GTT order: the double sale R6 exists to prevent.

**Fix (no new design decision).** R6 condition (e) item 4 already states that an explicit deleted/cancelled status
proves only that untriggered legs cannot execute and that "items 1–3, 5 and 6 still apply to every order already
triggered". `protection_exposure` now applies that to removal by (a): a deleted GTT with any triggered leg leaves 𝒢
only if every triggered order is visible, terminal with nothing left, and reconciled; otherwise it stays with
w = Σ r(o). Independently, the engine applies the existing E3 rule (any SELL order on the instrument not proven
terminal refuses the sale), which R6 cites as a precondition but which lives in the not-yet-wired `kill_switch`.
Both are covered (X21, E26, E28) and each is caught by its own mutation (M6, M7).

## 5. Mutations (11, all caught)

grace from initiation · acknowledgement as confirmation · missing as gone · rev. 1 exclusive-only rule · no
contradiction finding · no E3 check · deleted-after-trigger removed by (a) · unusable R_EE establishes 𝒢 · exit owner
kept after refusal · SELL re-sent after an ambiguous outcome · no supersede overlay.

Not mutated (disclosed): the "R_EE started before T_last" check is defensive (R_EE is always taken after the wait in
this code), so no test can make it decisive; the single-flight lock is backed by the DB exit-owner index (E17/E18 pass
with either).

## 6. Results and limitations

`tests/test_protection_s7.py` **32 passed**; with S4–S6: all engine modules pass (counts in the S9 report).
Unverified broker behaviour: B-04 (whether Kite stops two SELLs on the same shares — never relied on), B-28, B-31
(sets the grace), B-32, OCO, DDPI (B-05). The attribution hook is report-only until Phase 7 (no baseline).
