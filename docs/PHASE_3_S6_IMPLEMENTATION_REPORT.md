# Phase 3 Stage S6: implementation report (GTT observation, reconciliation and exposure aggregation)

**Date:** 2026-11-11 · **Branch / HEAD:** unchanged · **Specification:** R6 rows 9–18 and 23, DD-8 rev. 2 (R2-5),
M-2, M-3, M-5, M-7 R3-2 / R4 / R5-1 / R5-2 / R6, R3-3 conditions (a)–(e)

**Result: S6 accepted.** Dark only; FakeKite and tmp databases only.

## 1. Files

| File | Status | Purpose |
|---|---|---|
| `backend/protection_reconcile.py` | new | `reconcile_op` (rows 9–18), the exact settle rule, `late_match_watch` (row 23, DD-7 rev. 2), `m2_conditions`, `linked_gtt_watch` (M-5) |
| `backend/protection_exposure.py` | new | `evaluate()`: the nine-source set 𝒢, dedup, w(m), S_agg, T(m)/O(m), removal by (a)–(e) |
| `tests/test_protection_s6.py` | new | 40 tests |

## 2. Reconciliation (never from an incomplete read; never "missing = gone")

| Row | Rule | Test |
|---|---|---|
| 9 / 10 | own-epoch `sending` within the grace unchanged; past it, or another epoch → `unknown` with the settle count reset | R01 |
| 11–13 | read-back: full match → `confirmed` (+ derived protection state); wrong instrument / BUY leg / non-CNC / id linked elsewhere → `needs_review`; not listed or stale values → keep reading | R02–R04 |
| 14 / 18 | exactly one fingerprint match and no other unlinked active SELL GTT → `adopted` (DD-6 link CAS); two matches (B-24) or another unlinked SELL GTT → `needs_review` with every candidate | R05–R07 |
| 17 | settle: T0 = max(sent_at + SETTLE, unknown_at, takeover, resume); R2.started_at − R1.gtts_at ≥ spacing, and the same on the monotonic clock within one process; a resume/epoch change restarts the count | R08–R10 |
| 15 / 15a / 15b | cancel: explicit deleted/cancelled → `confirmed`; triggered or unknown status → `needs_review`; missing → settle, then `needs_review` (never confirmed, B-28). Modify: requested values → `confirmed`; missing/other status → `needs_review` | R11–R13 |
| 23 | a late matching GTT after `absent_after_settle` (or a `no_app_gtt` id reappearing) → URGENT `LATE_GTT_APPEARED`, position `PROTECTION_NEEDS_REVIEW`, never deleted; flat position: automatic `position_closed` cancel | R14 |
| M-2 | the four conditions in one fresh read; an adopted id is never app-proven | R15 |
| M-5 | a missing linked GTT is never unlinked or re-created; `LINKED_GTT_MISSING` after two spaced complete reads; explicit external delete → `PROTECTION_FAILED` + `GTT_REMOVED_EXTERNALLY` | R16 |

## 3. Exposure model (R3-2, R4, R5-1, R5-2, R6)

`S_agg = Σ w(m)`, `w(m) = max{L(v) : v ∈ V(m)} + Σ{r(o) : o ∈ O(m)}`, computed per (account, mode, instrument).

- **Membership** — sources 1–9 exactly as R3-2 (source 9 also covers a closed position's undischarged link and
  unresolved findings carrying a GTT id). Sources are kept per member for the refusal listing.
- **Dedup** — canonical GTT id (`normalise_gtt_id`); id-less creates `op:<id>`, never merged; conflicting ids, a
  malformed id, an id linked twice or a duplicate id in the listing → not established (fail closed).
- **V(m)** — the listed version, every create payload recorded for the id, and the old (pre-state recorded at send)
  and requested version of every unresolved modify. Combined by maximum. A version that cannot be established → UNKNOWN.
- **L(v)** — every potentially executable SELL leg summed (no OCO assumption); a leg whose triggered order is visible
  counts through r(o); a triggered leg without a visible order counts once at its full quantity (B-13); if which leg
  fired is unknown, every SELL leg counts.
- **T(m) / O(m)** — T(m) every triggered order (visible or expected); O(m) the ones that can still execute. A terminal
  order with nothing left contributes 0 and stays in T(m). An unfilled remainder counts as cancelled only when the
  broker reports the order CANCELLED/REJECTED; an unknown order status is UNKNOWN (B-32).
- **Removal** — only (a)–(e). (e) rev. 6 requires T(m) complete, every order terminal with zero remaining, every SELL
  leg proven dead, every fill recorded on an app exit with the same order id and quantity, and no unresolved modify.
- **Never capped** at holdings or the open quantity. A confirmed cancel contradicted by the read is reported (D/G2).

## 4. R6 examples → tests

| R6 example | Test | Result |
|---|---|---|
| same id via three sources counts once | X01 | G_max 1 |
| different ids, identical payloads | X02 | G_max 2, S_agg summed |
| T4 id-less create (B-24) | X03 | `op:<id>`, S_agg UNKNOWN (not 20, not "≥ 20") |
| T4 positive control (X and Y identified) | X04 | two members, summed |
| T1 two SELL legs on a position of 100 | X05 | w = 200, not capped |
| T2 unresolved multi-leg modify (both orders) | X06, X06b | 200 / 240; never 380/390; missing version → UNKNOWN |
| T3 two outstanding triggered orders; partial fill | X07 | 40 + 40 (remaining, not original) |
| terminal orders, fills reconciled / not | X08 | removed by (e) only after reconciliation |
| T5 one terminal leg of a two-leg GTT; unknown order status | X09 | kept with 100; UNKNOWN |
| T6 expected order not yet visible | X10 | kept; counted once at leg quantity |
| missing remaining quantity | X11 | UNKNOWN |
| incomplete / paginated listing | X12 | the local member stays (journal payload) |
| unresolved cancellation; explicit delete | X13 | kept (G4); removed by (a) |
| confirmed cancel contradicted by ACTIVE | X14 | kept + contradiction; (b) once not listed |
| unknown / conflicting GTT identity | X15 | not established |
| no read / incomplete read | X16 | not established |
| unlinked user GTTs; no holdings cap | X17 | G_max 2, S_agg 20 |
| unfilled remainder inferred cancelled | X19 | kept until CANCELLED is reported |
| triggered, no leg result (empty O(m)) | X20 | kept; every leg counts |
| clean instrument | X18 | gate open |

## 5. Tests and mutations

`tests/test_protection_s6.py`: **41 passed** (37 + X06b, X19, X20 added during mutation analysis, + X21 added in S7
for the deleted-after-trigger defect).

24 mutations, all caught after two test gaps were closed: count one leg; maximum of legs; sum versions; requested
version only (**survived at first**, X06b added); count one order; original order quantity; id-less payload sum; merge
by fingerprint; (e) over O(m) only (**survived at first**: the mutation left a redundant guard in place; restated
faithfully, and X20 added); skip fill reconciliation; ignore the expected order; count a terminal order; infer an
unfilled remainder cancelled; drop on a cancel acknowledgement; ignore listed unlinked GTTs; cap at holdings; missing =
confirmed; adopt with another unlinked GTT; T0 without SETTLE; no spacing; no G4 reset; transition on an incomplete
read; no monotonic spacing; OCO assumed.

## 6. Acceptance and limitations

All S6 criteria met. Limitations (all fail-closed, all broker facts): listing completeness and pagination (B-08),
retention of deleted GTTs (B-28), triggered-order visibility (B-13), OCO behaviour (not relied on), status vocabulary
(B-32), id reuse (B-29). `LINKED_GTT_MISSING` counting is process memory (a restart only delays the finding).
