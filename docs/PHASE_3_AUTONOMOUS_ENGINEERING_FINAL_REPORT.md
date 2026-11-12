# Phase 3 autonomous engineering programme (S4–S9): final report

**Date:** 2026-11-11 · **Repository:** `D:\Claude_WS\kite-terminal` · **Branch:** `feature/signal-detail-and-strategy-revision`
· **HEAD:** `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (unchanged; nothing staged, committed, pushed or stashed)
· **Specification:** `docs/PHASE_3_DESIGN_DECISIONS.md` revision 6 ("R6")

Phase reports: `PHASE_3_S4_IMPLEMENTATION_REPORT.md`, `…_S5_…`, `…_S6_…`, `…_S7_…`, `…_S8_…`,
`PHASE_3_S9_VALIDATION_REPORT.md`, `PHASE_9_KITE_VALIDATION_MATRIX.md`, `PHASE_3_V12_FINAL_REGISTRATION_PROPOSAL.md`.

---

## A. Executive summary

**Completed (dark, offline, FakeKite and temporary databases only):**

- **S4** the protection journal and state machine (`backend/protection.py`): the §4.2 transition table as fenced
  compare-and-set, DD-5 claims, DD-7 events, the DX rev. 3 classifier, derived protection state, Q3/R2-6 manual
  resolution, the Q1 supersede overlay and §4.3 resolution.
- **S5** the fenced send (`backend/protection_send.py`): write-ahead `sending`, then the broker call inside a fenced
  transaction **held across the call**, so no takeover can commit mid-send; M-4 response association by
  `(op_id, op_uuid, attempt, state)`; ambiguous outcomes never re-sent; G2 logging; M-3 retries.
- **S6** reconciliation and exposure (`backend/protection_reconcile.py`, `backend/protection_exposure.py`): rows 9–18,
  the exact DD-8 settle rule, late-GTT watch, M-2, M-5; the nine-source set 𝒢, G_max, S_agg = Σ w(m) with R4 versions,
  R5-1 id-less creates, and R6 T(m)/O(m).
- **S7** the Emergency Exit engine API (`backend/emergency_exit_engine.py`): owner claim + supersede + app-proven
  cancels in one transaction, grace from T_last, one fresh R_EE, the full gate, write-ahead fenced SELL, no override,
  no deferred sale.
- **S8** runtime continuity (`backend/instance_lock.py` M-6/R2-7 primitives, `backend/protection_runtime.py`): shared
  clock baseline with exactly-once resume detection, the heartbeat consuming any unnoted resume, the G4
  consumer-driven reset, DD-9, journal-aware recovery, G3 inventory.
- **S9** multi-process engine tests on the S3 harness, static safety checks, repeated race runs, full regression,
  integrity verification.
- **Phase 9 preparation** (broker-validation matrix) and the **v12 registration proposal**, including a resolution of
  the DD-6/R2-4 predicate question (derivable from authoritative code; awaiting user approval).

**Not completed / not authorised (by design):** v12 registration; any wiring into live paths (`routes/kill_switch`,
`routes/tickets`, `order_monitor`, `trailing_engine`); closing the two **legacy** fencing residuals (MP-E2, MP-E3);
the M-6 (c) legacy resume detection; any broker contact; Phase 4 (`execution.py`).

**Defect found and fixed during the programme:** the S7 tests caught the engine selling against a GTT that had
already triggered and was then deleted (its SELL order still open). Fixed by applying R6 condition (e) item 4 to
removal by (a), plus the engine-side E3 outstanding-SELL check (§F).

**S9 design conflict (§J B-D1) resolved by the user's option A:** the first full regression failed 2 pre-existing
structural tests ("only `execution.py` places orders/GTTs"; "no module uses the dark schema") that the R6 engine
cannot satisfy as written. On the user's decision, both now allow exactly the dark engine's two fenced writers and its
schema use, and still forbid every other module (mutation-checked). **The full regression is green: 4178 passed,
0 failed, 0 errors, 6 known skips.**

**Overall readiness:** the offline engine is implemented and strongly tested against R6. It is **not** ready for
live money: live-activation blockers remain in broker evidence, legacy-path fencing, wiring and registration (§J).

## B. Phase-by-phase status

| Phase | Scope | Files (new / modified) | Tests | Mutations | Decision |
|---|---|---|---|---|---|
| S4 | journal, transitions, claims, DX, state, resolution, overlay | `protection.py`, `protection_evidence.py` (new); `config.py` (append) | 157 | 13/13 | accepted |
| S5 | fenced send, M-4, G2, M-3 | `protection_send.py`, `protection_runtime.py` (new); `instance_lock.py` (edit) | 36 | 12/12 | accepted (engine); legacy residuals open |
| S6 | reconcile, settle, 𝒢/S_agg/T/O | `protection_reconcile.py`, `protection_exposure.py` (new) | 41 | 24/24 (2 survivors fixed by new tests) | accepted |
| S7 | Emergency Exit engine | `emergency_exit_engine.py` (new) | 32 | 11/11 | accepted; 1 defect fixed |
| S8 | clocks, resume, recovery, DD-9, G3, G4 | `protection_runtime.py`; `instance_lock.py`, `reconciliation.py`, `startup_reconciliation.py` (edits) | 27 | 11/11 | accepted (offline scope) |
| S9 | MP engine tests, static checks, regression, integrity | `test_protection_mp.py`, `test_protection_s9_static.py` (new); `mp_app_child.py` (additions); `mp_harness.py` (fix) | 7 MP (×5 repeats) + 6 static | 3 (option A) | **accepted** after option A (full regression green) |

Every pre-existing file was edited with the Edit tool (line endings preserved); the complete list is in §I.

## C. R6 requirement traceability

| Req. | R6 reference | Implementation | Test(s) | Result | Remaining limitation |
|---|---|---|---|---|---|
| DX | §2, R2-1, R3-1 | `protection.classify`, `protection_send.evaluate_dx`/`dx_permits` | S4 T24–T26; S5 T11 | pass | cap needs a baseline (Phase 7): `attribution_cap` is never `caps_sell_exposure` yet (fail-closed) |
| DD-1 | DDL | S1 (pre-existing) | S4 T01, T11, T18 | pass | — |
| DD-2 | v12 dark | `MIGRATIONS` unchanged | S4 T23; ST5 | pass | registration is a separate gate |
| DD-3 | reasons | `protection.REASONS` | S4 T18, T36 | pass | — |
| DD-4 | direction / retry | `direction_of`; DDL CHECK; `dx_permits` | S4 T27, T18; S5 T03, T28 | pass | — |
| DD-5 | CAS, INSERT…SELECT, single writer | `transition`, `claim`, `journal_transaction` | S4 T01–T06, T21, T35; ST6 | pass | — |
| DD-6 | link guard; preflight | `link_gtt`; proposal §3 | S4 T19, T20 | pass | preflight predicate awaits approval |
| DD-7 | derived cleanup, events | `derive_protection`, `EVENT_FOR_STATE` | S4 T01, T29, T37 | pass | — |
| DD-8 | settle/freshness | `freshness_problem`, `settle_counts`, `settle_t0` | S5 T09; S6 R08–R10 | pass | values are placeholders (Phase 9) |
| DD-9 | exposure predicate | `protection_runtime.may_increase_exposure` | S8 C15, C16 (×7) | pass | not yet called by a live claim path (no wiring) |
| DD-10 | G1, G2, G4 | `send_gate`/`pre_call_gate`; `g2_log`; `reset_memory_if_resumed` | S5 T17–T19; S8 C18 | pass | G1 legacy extension (M-6 c) not done |
| DD-11 | dry run = tests | paper broker on the engine's connection | S5 T31 | pass | — |
| Q1 | overlay | `supersede_open_changes`, `resolve_superseded_result` | S4 T34; S5 T06, T07; S7 E23, E24 | pass | — |
| Q2 | absent_after_settle | DDL + settle | S6 R08 | pass | — |
| Q3 / R2-6 | manual resolution | `resolve_manual` | S4 T30–T33 | pass | UI/route is Phase 8 |
| M-1 / R2-2 | cancel by effect | `dx_permits`, `flat_cancel_action`, reclaim bound | S4 T17; S5 T26–T29 | pass | — |
| M-2 | duplicate vs sole GTT | `m2_conditions` | S5 T11; S6 R15 | pass | — |
| M-3 | no blind retries | `retry_problem`, `retry` | S5 T03, T26–T29 | pass | B-28 may later relax row 15a |
| M-4 | late responses | `record_response`, `record_late_response` | S5 T20–T25; S8 C10 | pass | — |
| M-5 | no create on absence | `create_claim_problem`, `linked_gtt_watch` | S4 T12–T15; S6 R16; MP-PE3 | pass | missing-count is process memory |
| M-6 / R2-7 | resume gating | `instance_lock` primitives, `send_gate` | S8 C01–C12 | pass | legacy-path detection (c) not done; physical WIN-INT pending |
| M-7 R3-2 | 𝒢 membership, dedup | `protection_exposure.evaluate` | S6 X01–X04, X12, X15, X17 | pass | B-08 residual |
| M-7 R3-3 | grace, R_EE, G1–G22 | `emergency_exit_engine._run` | S7 E01–E28 | pass | grace value is B-31 |
| M-7 R4 | w(m) | `listed_terms`, versions | S6 X05–X07, X06b | pass | — |
| M-7 R5-1 | id-less create | `op:<id>` w UNKNOWN | S6 X03, X04; S7 E11 | pass | — |
| M-7 R5-2 / R6 | (e), T(m), O(m) | `listed_terms`, `e_holds`, deleted-after-trigger rule | S6 X08–X11, X19–X21; S7 E09, E10, E26 | pass | — |
| M-8 | absent CHECK | DDL | S4 T01 | pass | — |
| M-9 | v12 | S2 (pre-existing) | regression (`test_schema_v12.py`) | pass | registration not done |
| M-10 | broker register | Phase 9 matrix | — | prepared | all unverified |

## D. Concurrency and execution safety

- **Ownership and fencing.** Every journal write runs inside `instance_lock.fenced_transaction` (BEGIN IMMEDIATE, epoch
  check under the write lock) via `journal_transaction`, whose marker every write function checks (S4 T21; ST6).
- **Broker-write authorisation.** Transaction A commits `sending` (write-ahead). Transaction B is held across the call:
  epoch, op identity, attempt and the pre-call gate are checked inside it, and the response is recorded before COMMIT.
  A takeover cannot commit during a send (S5 T16, MP-PE2 across real processes). Residual: only broker-side processing
  after a client timeout (B-31), handled as ambiguous.
- **Late responses.** Associated by `(op_id, op_uuid, attempt, state)`; non-current attempts, duplicates and reordered
  responses are evidence only; contradictory responses to terminal ops raise URGENT findings (S5 T20–T25).
- **Ambiguous operations.** `unknown`, never re-sent for create / modify up / loosen; cancels and down/tighten retried
  only after a post-grace read shows the pre-state (M-3).
- **Takeover.** A stale owner can neither claim nor send (MP-PE1); a crash after the broker effect leaves `sending`,
  the successor recovers it to `unknown` and adopts by fingerprint with zero broker writes of its own (MP-PE3, PE4).
- **Legacy residuals (open).** MP-E2 (an unfenced legacy loop write commits after a takeover) and MP-E3 (legacy
  `broker_write` checks the in-memory role only). Unchanged by design; see §J B-L1/B-L2.

## E. GTT and protection correctness

GTT identity by canonical id (`normalise_gtt_id`); id-less creates never merged; conflicting/malformed ids fail
closed. G_max = |𝒢|; S_agg = Σ w(m) never capped; w(m) = max over versions + Σ r(o) over O(m); T(m) decides removal
under (e); a GTT deleted after it triggered keeps its outstanding orders. Triggered orders, partial fills and terminal
orders per §3 of the S6 report; fills must be reconciled against app exits with the same order id and quantity.
Reconciliation never concludes "absent" from an incomplete read and never treats "missing" as "gone"; manual review
(`needs_review`) is reached by every contradiction and resolved only by a human with a fresh complete read.

## F. Emergency Exit

**Gate:** preconditions → one fenced transaction (owner, overlay, app-proven cancels) → cancels sent once → grace from
T_last (early end only on `confirmed`/`rejected`) → one fresh complete R_EE after initiation and T_last → 𝒢
established, G_max = 0, S_agg = 0, attribution exclusive/shared, no outstanding SELL order on the instrument → SELL
(write-ahead, fenced, never re-sent if ambiguous) and the POSSIBLE_DOUBLE_SELL watch. Otherwise `refused`, listing every
member with its sources and w; the never-sent exit ticket is cancelled. No override; no deferred or background sale
(static test). Refusal tests: S7 E04–E14, E19–E22, E26–E28; the grace semantics E15/E16; concurrency E17/E18 and
MP-PE6 (two processes, one SELL).

**Defect fixed:** E09/E10 first sold against a triggered-then-deleted GTT whose order was open. Removal by (a) now
requires every already-triggered order to be terminal with nothing left and reconciled (R6 (e) item 4), and the
engine applies the E3 outstanding-SELL rule. Regression tests X21, E26, E28; mutations S7-M6/M7 caught.

## G. Runtime safety

Heartbeat and engine share one clock baseline; one `resume_seq` per gap whichever detector sees it; the heartbeat
loop notes any unnoted resume (persist + Stage 1); sends gate on the shared flag; pre-suspend reads are refused;
backward clock steps fail safe through read freshness; authority is never regained in-process; recovery moves
older-epoch `intent` → `not_sent`, `sending` → `unknown`, releases never-submitted EE tickets, never a submitted one.
Real sleep/hibernate ordering, monotonic behaviour across sleep, and NTP behaviour need a physical Windows run
(S8 report §6).

## H. Testing and quality

- **Full standing regression (final, after the v12 preparation work): exit 0 — 4199 collected, 4193 passed,
  0 failed, 0 errors, 6 skipped (JUnit); children: 155 reports, 0 unexpected, 0 orphans.** In between: the
  `rollback-schema-v12` CLI was withdrawn (R6 M-9; `test_a2`), and backup sidecars (`*_bak-wal`/`-shm`/`-journal`)
  were added to `.gitignore` after the operator's read-only checks created them. (After the preflight command
  alone: 4189 passed of 4195.) (After option A alone:
  4178 passed of 4184.) Baseline before this programme: 3872 passed, 0 failed, 6 skipped.
- First full run (before option A): 4169 passed, 2 failed (the B-D1 conflict), 1 error (a harness teardown defect:
  pid reuse made an unrelated security process look like a child and teardown tried to kill it — fixed in
  `tests/mp_harness.py`).
- Focused engine suites: S4 157, S5 36, S6 41, S7 32, S8 27, static 6 = **299 passed** (JUnit); MP 7 passed.
- Repeated races: `test_protection_mp.py` + S3 MP-L1/MP-C1/MP-E1/residuals, 5 consecutive runs, all passed; child
  guard 45 reports per run, 0 unexpected refusals, 0 protected opens, 0 orphan processes.
- Mutation testing: 71 mutations across S4–S8 (two production-file mutations of `instance_lock.py`, restored with
  sha256 verified); all caught after closing two test gaps in S6.
- Skips: the six pre-existing environment skips only (three WIN-INT placeholders, three symlink-privilege).
- Guard: parent `unexpected 0`, network refusals 0 in every run.

## I. Security and integrity

| Check | Before | After |
|---|---|---|
| Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07f…` | unchanged |
| Staged / stash | empty / empty | empty / empty |
| Worktree entries | 112 | 136 (24 added, 8 changed, 104 byte-identical) |
| PK-25 frozen files | 13/13 | 13/13 |
| `backend/execution.py` | identical to HEAD | identical to HEAD |
| `LIVE_ENTRIES_ENABLED` | `False` | `False` |
| `MIGRATIONS` | ends at v11 | ends at v11 (v12 **not** registered) |

**Files changed by this programme.** Added (backend): `protection.py`, `protection_evidence.py`, `protection_send.py`,
`protection_runtime.py`, `protection_reconcile.py`, `protection_exposure.py`, `emergency_exit_engine.py`. Changed
(backend, surgical Edit): `config.py` (DD-8 placeholders appended), `instance_lock.py` (M-6/R2-7 primitives),
`reconciliation.py` (G4 `reset_memory_if_resumed`), `startup_reconciliation.py` (G4 call; G3 inventory). Added
(tests): `protection_helpers.py`, `test_protection_s4.py` … `test_protection_s8.py`, `test_protection_mp.py`,
`test_protection_s9_static.py`. Changed (tests): `mp_app_child.py` (engine commands, additions only), `mp_harness.py`
(descendant creation-time check), `test_live_entry_gate.py` (test_15) and `test_dark_protection_schema.py` (test_11)
per option A. Later (user request): `backend/db_maintenance.py` (`preflight-v12` command) and
`tests/test_preflight_v12.py`; then `backend/db.py` (`_require_dark_history`), `backend/db_maintenance.py`
(`rollback-schema-v12`) and `tests/test_v12_registration_prep.py`. Added (docs): the eight reports listed at the top.

**Protected data:** `.env*`, credentials, the account database, its backups, its lock file and `backend/ui_prefs.json`
were never opened, read, hashed or stat'ed (the integrity script skips them by pattern; the guard reported 0
unexpected refusals in the parent and every child). **Broker/network:** no Kite contact, no Kite MCP tool, no external
network request; only loopback (the S3 harness). The real application was never started; every database was a pytest
temporary database. Two temporary production-file mutations (`instance_lock.py`, S5-M9 and S8) were restored with
sha256 verified.

## J. Outstanding risks

| ID | Class | Risk |
|---|---|---|
| B-D1 | RESOLVED (option A) / HIGH for live wiring | test_15 and test_11 now allow exactly the dark engine's two fenced writers and its schema use; every other module stays forbidden, and the engine is imported by no live module (mutation-checked). For live wiring, option B (engine writes through `execution.py`, Phase 4) remains the long-term question. |
| B-L1 | **BLOCKER** | Legacy DB fencing residual (MP-E2): a legacy loop past `loop_may_run` commits an unfenced write after a takeover. Proposal: install a per-connection TEMP trigger fence (refuse writes when `app_instance` is not the connection owner's epoch) in `db.get_connection`, or move legacy writers onto fenced transactions. Needs authorisation (live-path change). |
| B-L2 | **BLOCKER** | Legacy broker-write residual (MP-E3): `broker_write` trusts the in-memory role. Proposal: re-read `app_instance` before admitting a legacy write (narrows the window to milliseconds), and route Phase 4–6 writes through the engine's held-transaction pattern (closes it). |
| B-W1 | **BLOCKER** | No wiring: Emergency Exit (Phase 6), protection upkeep (Phases 4–5) still use legacy code; `execution.py` change (Phase 4) needs its own authorisation. |
| B-R1 | **BLOCKER** | v12 not registered; operator backup not taken; preflight not yet run on it (the R2-4 predicate is approved). |
| B-K1..6 | **BLOCKER** | Broker evidence: DDPI/TPIN (B-05), placement after fill (B-02), holdings at trigger / T1 (B-03), over-quantity GTT (B-04), listing completeness (B-08, DD-8 values), proxy replay (B-22). |
| B-M6 | **BLOCKER** | M-6 (c) legacy resume detection not done. |
| H-1 | HIGH | Attribution is report-only (no baseline before Phase 7): `caps_sell_exposure` never applies; EE relies on report-only attribution. |
| H-2 | HIGH | Holding the fenced transaction across the broker call blocks other writers for up to one call (≤ 7 s SDK timeout, under the 10 s busy timeout); a frozen process blocks a takeover until it resumes (fail-closed). Needs load observation in Phase 9/10. |
| M-1 | MEDIUM | `LINKED_GTT_MISSING` counting is process memory (a restart delays the finding). |
| M-2 | MEDIUM | DD-8 values are placeholders; the grace (B-31) bounds the EE wait. |
| M-3 | MEDIUM | Physical WIN-INT validation of sleep/hibernate, monotonic clock and lock behaviour is outstanding. |
| L-1 | LOW | New engine files are LF (the repository mixes CRLF and LF; edited pre-existing files kept their endings). |

## K. Kite validation

See `docs/PHASE_9_KITE_VALIDATION_MATRIX.md`: 20 behaviours, each with the R6 requirement, the FakeKite model, what is
known (nothing is verified for this account), the safe method (DOC / RO / supervised one-share S1 / none), the evidence
needed, the risk, and whether the app can operate without it. Six are live-money blockers.

## L. Migration v12

`docs/PHASE_3_V12_FINAL_REGISTRATION_PROPOSAL.md`: the exact one-line registration plus three companion changes; the
preconditions; backup, integrity, expected schema, rollback order and procedure, failure recovery, compatibility and
operator confirmations. **DD-6/R2-4:** the predicate is derivable without invention from the application's one
authoritative open-position definition (`risk_engine._OPEN_POSITION_WHERE`) with `mode IS NULL`; **approved by the
user as proposed on 2026-11-11.** **Registration was not performed**; no migration, rollback or preflight ran against any real database.

## M. Final release recommendation

**NOT READY FOR LIVE TRADING.**

The offline engine meets the R6 design under strong automated evidence, but live-money use is blocked by unverified
broker behaviour (six LIVE-BLOCKER items), the open legacy fencing residuals, the absence of wiring and v12
registration, and the outstanding M-6 (c) and WIN-INT work. Passing FakeKite tests establish none of the broker facts.

### Decisions needed from the user

0. ~~Resolve B-D1~~ — done: option A chosen; regression green. For Phase 4/6 wiring, decide whether engine writes
   move behind `execution.py` (option B).
1. ~~Approve the R2-4 preflight predicate~~ — approved as proposed on 2026-11-11 (v12 proposal §3). The read-only
   `db_maintenance preflight-v12 --db <backup>` command is built and tested on temporary databases (v12 proposal §12).
   Running it on the operator backup and registering v12 remain separate steps. The other two companion changes
   are also done offline (v12 proposal §1 items 1–2): the dark entry points refuse a v12 history, and
   `db_maintenance rollback-schema-v12 --confirm` exists.
2. Authorise, or decline, the legacy fencing changes B-L1 / B-L2 (live-path behaviour).
3. Authorise the M-6 (c) legacy resume detection.
4. Authorise Phase 9 broker validation (read-only first, then supervised one-share experiments).
5. Later, separately: v12 registration with the operator backup; Phase 4 (`execution.py`); Phase 6 wiring.
