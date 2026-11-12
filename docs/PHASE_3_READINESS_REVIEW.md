# Phase 3 readiness review: protection engine (offline engineering) and live activation

**Status: READ-ONLY ASSESSMENT. Nothing is implemented, nothing is enabled.**
This document changes no code, test, configuration, schema or database. It does not authorise Phase 3
implementation, any migration, any broker operation or live trading.
`backend/live_entry_gate.LIVE_ENTRIES_ENABLED` **stays `False`**.

Citations used throughout:

- **D§n** = `docs/PHASE_2_DESIGN_REVIEW.md` (D:10 defines the next abbreviation);
- **P§n** = `docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md`;
- **P1-Qn** = `docs/PHASE_1_DARK_SCHEMA_REPORT.md` §8;
- **Ph2** = `docs/PHASE_2_IMPLEMENTATION_REPORT.md`; **WIN** = `docs/PHASE_2_WINDOWS_VALIDATION_REPORT.md`;
- **U6I** = `docs/U6_PROXY_REPLAY_INVESTIGATION.md`; **U6M** = `docs/U6_MITIGATION_REPORT.md`.

### Evidence labels

| Label | Meaning |
|---|---|
| **[V]** | Verified from the repository source or docs in this session, with a file reference |
| **[T]** | Test evidence **reported** in an existing report (not re-run in this session) |
| **[I]** | Inference: reasoned, not proven |
| **[U]** | Unknown, or dependent on external/broker behaviour |

### Classification categories (as required)

**BLOCKER — PHASE 3 ENGINEERING** · **BLOCKER — LIVE ACTIVATION ONLY** · **REQUIRED DESIGN DECISION** ·
**REQUIRED OFFLINE TEST** · **SUPPLEMENTARY VALIDATION** · **UNVERIFIED — EXTERNAL/BROKER EVIDENCE REQUIRED** ·
**NOT A BLOCKER**

"Phase 3" here means P§12 Phase 3: **the protection engine, dark** — `backend/protection.py` with create, adopt,
modify, cancel and recovery, "invoked only by tests and for dry run", using FakeKite and scratch databases only
(P:1224). Wiring the engine into live paths is P§12 Phases 4–6, each separately authorised.

---

## 1. Executive decision: Phase 3 engineering

### **CONDITIONAL GO**

The protection engine can be implemented and tested **entirely offline** (isolated scratch databases, FakeKite,
the Phase 0 guard, the Phase 2 fencing primitives) without enabling real trading. No broker fact is needed to
*build and test* a fail-closed engine; the broker unknowns only fix parameters and decide whether the engine is
*sufficient* for live use. [I, from §§6–10]

Conditions, all of which must hold before the first line of Phase 3 code:

1. **The D§11.2 amendments are decided and applied first** to the dark DDL (§6). Today's dark schema still has
   `abandoned`/`superseded` and a single open-operation index (`backend/db.py:2538-2603` [V]); an engine built on
   it would encode the Q1–Q3 defects that D§3 identified.
2. **v12 stays dark (unregistered) during Phase 3** (§7). The Phase 1 functions are renumbered to `_m012_*` /
   `_r012_*` but not added to `MIGRATIONS`, so no build can migrate the real account database. Registration is a
   separate governance gate (like U11).
3. **Scope is the dark engine only**: no wiring into `order_monitor`, `trailing_engine`, `routes/kill_switch`,
   `routes/tickets` or any live path; no change to `backend/execution.py` or any PK-25 file.
4. **Phase 3 includes** the resume-latency claim guard and a journal-aware Stage 1 inventory (§10, gaps G1/G3),
   and the late-response logging rule after authority loss (gap G2).
5. **Harness extensions are built first** (shared-state multi-process FakeKite, day rollover, clock injection)
   (§4, §13 S3).

The decisions in §12 are owned by the user; this review recommends but does not resolve them.

## 2. Separate decision: live-trading activation

### **NO-GO**

`LIVE_ENTRIES_ENABLED = False` (`backend/live_entry_gate.py:40` [V]) must stay. Live activation is blocked by
external broker facts that no offline test can establish (U1 = P§10 item 6; P§10 items 3, 4, 5, 9), by the
separately authorised Phase 4 change to the frozen `execution.py`, by the separately authorised Phase 9 broker
validation, and by operational preconditions (§5). This agrees with D§14 (D:1270-1271), Ph2 §15, WIN §9 and
U6M §9.

---

## 3. Repository state and evidence inspected

### 3.1 Recorded starting state (2026-11-09)

| Item | Value | How established |
|---|---|---|
| Repository | `D:/Claude_WS/kite-terminal` | `pwd` [V] |
| Branch | `feature/signal-detail-and-strategy-revision` (expected) | `git rev-parse --abbrev-ref HEAD` [V] |
| HEAD | `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (expected) | `git rev-parse HEAD` [V] |
| Working tree | 99 entries: 72 ` M`, 27 `??`; nothing staged; stash empty. Saved to the session scratchpad (`status_before.txt`), with a `git hash-object` of every entry (`dirty_hashes_before.txt`) | `git --no-optional-locks status --porcelain=v1 --untracked-files=all` [V] |
| `backend/execution.py` | blob `fc8e1ba0…` = `HEAD:backend/execution.py` | `git hash-object` vs `git rev-parse HEAD:…` [V] |
| PK-25 frozen hashes | **13 / 13 match** (11 production + 2 model; sha256 with CRLF→LF; `research/pk25/validation_manifest.json` → `model_freeze`) | scratchpad `pk25.py`, read-only, no `backend` import [V] |
| `LIVE_ENTRIES_ENABLED` | `False` | `grep`, `backend/live_entry_gate.py:40` [V] |

Git was invoked with `--no-optional-locks` so that `status` does not refresh the index.

**Access note.** The session's working directory was initially a different folder, and the Read/Grep tools were
blocked by `permissions.blockReadsOutsideWorkingDirectories`. The user authorised read-only Bash reads; the
repository was later added as a working directory, after which Read/Grep were used. No protected file was opened
by any route (§15).

### 3.2 Evidence inspected

| Document | Present? | Used for |
|---|---|---|
| `docs/PHASE_0_TEST_HARNESS_SAFETY_REPORT.md` | yes | harness guarantees, deferred harnesses (§7, §9) |
| `docs/PHASE_1_DARK_SCHEMA_REPORT.md` | yes | dark DDL, D1/D2, P1-Q1…Q10, compatibility (§7) |
| `docs/PHASE_2_DESIGN_REVIEW.md` (D) | yes | Q1–Q4 answers, §4 state model, §7 fencing, §8 Stage 1, §11.2 amendments, §12 open items |
| `docs/PHASE_2_IMPLEMENTATION_REPORT.md` | yes | what was built, tests, limitations, §13 prerequisites |
| `docs/PHASE_2_WINDOWS_VALIDATION_REPORT.md` | yes | L3/L6/R8 results and caveats |
| `docs/U6_PROXY_REPLAY_INVESTIGATION.md` | yes | replay analysis, §8 mitigations, §10 recommendation |
| `docs/U6_MITIGATION_REPORT.md` | yes | M1/M2, §8 remaining questions |
| `docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md` (P) | yes | §10 broker unknowns, §11 files, §12 phases, §13 risks |
| `docs/PROPOSAL_EXECUTION_GTT_AFTER_FILL.md` | yes | the only DDPI/TPIN source (line 18) |
| **Any Phase 3 planning document** | **absent** | `docs/` has no `PHASE_3*`; no `backend/protection.py`, `tests/test_protection_engine*.py` or `tests/test_protection_recovery*.py` (Glob) [V] |

Source and tests read: `backend/db.py` (migration registry, `migrate`, `_check_history`, `rollback_schema_v11`,
the dark section `:2514-2928`); `backend/instance_lock.py` (authority, fence, `fenced_transaction`, heartbeat,
resume); `backend/startup_reconciliation.py` (`derive_restriction`, `note_resume`, function inventory);
`backend/reconciliation.py` (cross-cycle memory); loop guards in `order_monitor.py`, `trailing_engine.py`,
`reconciliation.py`, `automation.py`, `data_jobs.py`; `tests/fake_kite.py` (fault and UNVERIFIED policies);
`tests/test_dark_protection_schema.py` (pins on the old enum and on `_m011_`); `tests/test_startup_reconciliation.py`
(F1/F2); `tests/winint_phase2.py` (R8 runner).

**No test was run in this session.** All test results below are [T], quoted from the reports.

### 3.3 Verified meaning of the identifiers

| Identifier | Definition in the repository | Location |
|---|---|---|
| **D§11.2 Q1–Q3** | "Amendments to the Phase 1 dark schema (for Q1–Q3)": a change table derived from D§3's answers to Q1 (`superseded`), Q2 (`abandoned`), Q3 (`needs_review` and the open index). Q1–Q3 originate as P1-Q1, P1-Q2, P1-Q3. | D:1135-1152; D:243-391; Phase 1 report §8 |
| **U1** | "DDPI / TPIN behaviour for GTT-triggered CNC delivery SELLs (unattended execution without DDPI; eDIS pre-authorisation with TPIN only)", label [B], = **P§10 item 6**; blocks live trading, not Phase 2 | D:1180; P:1156 |
| **P§10 item 3** | Can a CNC SELL GTT be **placed** before the purchased shares are visible in holdings or positions? | P:1153 |
| **P§10 item 4** | Does GTT **trigger and execution** depend on holdings availability at trigger time (T0 / T1 / settled)? | P:1154 |
| **P§10 item 5** | A triggered GTT whose quantity exceeds the sellable quantity: rejected, partial, or a short sale (auction risk)? | P:1155 |
| **P§10 item 9** | `get_gtts()` read-after-write consistency and list completeness (pagination, non-active GTTs) | P:1159 |

P§13 (P:1269-1270) says Phase 10 "should not be considered until §10 items 3, 4, 5, 6 and 9 have documented
answers"; D:1193 (U14) and D:1270 name items 3, 4, 5, 9 plus U1 (item 6) as live blockers. [V]

---

## 4. Phase 3 engineering blockers

Only items that must be resolved **before or as the first step of** Phase 3 engineering. Everything else is in §5
or is a decision/test inside Phase 3.

| # | Item | Evidence | Classification | Resolution |
|---|---|---|---|---|
| E-1 | **D§11.2 amendments not applied.** The dark DDL still defines `abandoned` and `superseded` as states, imposes no `sent_at` rule on them, and has one unpartitioned open index. | `db.py:2538-2546` (enum, open set), `:2569-2599` (DDL), `:2600-2603` (index) [V]; tests pin the old enum: `test_dark_protection_schema.py:325, 373, 456-458` [V] | **BLOCKER — PHASE 3 ENGINEERING** | Decide the §12 sub-items, then amend the dark DDL as stage S1 (§13). Cheap now: the table is unregistered and empty everywhere (D:1137-1139). |
| E-2 | **`_m011_*` / `_r011_*` collide with the registered v11 number.** v11 is taken by Phase 2; the dark functions must be renumbered before any registration and before Phase 3 code depends on their names. | `db.py:2220-2222` (comment: "Phase 3 renumbers to v12"), `:2256`, `:2838`, `:2855` [V]; identity and regex pins `test_dark_protection_schema.py:194, 688` [V] | **BLOCKER — PHASE 3 ENGINEERING** (mechanical) | Rename to `_m012_*` / `_r012_*` in S1, update the two pins. |
| E-3 | **The v12 registration decision.** Whether Phase 3 registers v12 (migrating the real DB on the next start) or keeps it dark. | D:1121-1131 (v11 precedent), Phase 1 D1 [V] | **REQUIRED DESIGN DECISION** (gates S1/S2) | Recommended: dark through Phase 3 (§7.4). |

Not blockers for starting Phase 3, but required inside it (§13): the shared-state FakeKite and day-rollover
harness (**REQUIRED OFFLINE TEST**, §10 G6), the resume claim guard (G1), journal-aware Stage 1 (G3),
late-response logging (G2).

---

## 5. Live-activation-only blockers

| # | Item | Evidence | Classification |
|---|---|---|---|
| L-1 | **U1 DDPI/TPIN** (P§10 item 6) | §8 | **BLOCKER — LIVE ACTIVATION ONLY**; **UNVERIFIED — EXTERNAL/BROKER EVIDENCE REQUIRED** |
| L-2 | **P§10 items 3, 4, 5, 9** | §9 | **BLOCKER — LIVE ACTIVATION ONLY**; **UNVERIFIED — EXTERNAL/BROKER EVIDENCE REQUIRED** |
| L-3 | **Phase 4: the frozen `execution.py` change** (remove GTT-at-acceptance `:480-540`; PK-25 supersede record). Without it, live entries keep today's GTT-at-acceptance path; the engine cannot protect live entries. | P§11 (P:1194-1195), P§12 row 4 (P:1225) [V] | **BLOCKER — LIVE ACTIVATION ONLY** (separate authorisation) |
| L-4 | **Phase 9 broker validation** (P§10 items 3–11, 14, 15; read-only reads, then one supervised one-share trade) and **Phase 10 gate decision** | P§12 rows 9–10 (P:1230-1231) [V] | **BLOCKER — LIVE ACTIVATION ONLY** (separate authorisations) |
| L-5 | **Operator backup before the first real start** of the v11 build (and again before any v12 registration). Not done. | Ph2 §11, §15 ("has not been done"); WIN §7 [V] | **BLOCKER — LIVE ACTIVATION ONLY** (and a precondition of *any* real app start of this worktree, since v11 is registered) |
| L-6 | **U6 remaining external items**: Squid config (`ssl_bump`, `forward_max_tries`, `retry_on_error`); Kite edge retries to OMS; no idempotency key; 5xx semantics; repeated `modify_gtt`/`delete_gtt` semantics | U6M §8 items 1–6 [V] | **UNVERIFIED — EXTERNAL/BROKER EVIDENCE REQUIRED**; live-only. Offline, the engine must treat each as unknown (item 6 affects D§4.2 retry classes) |
| L-7 | **Broker reconciliation after a real resume** was never executed (Stage 1 after resume is covered only by a simulated test) | WIN §3.5 ("not executed here"), §8 item 4 [V] | **UNVERIFIED — EXTERNAL/BROKER EVIDENCE REQUIRED**; Phase 9 |
| L-8 | U8 GTT completeness/lag/`created_at` (P§10 items 9–11) fixes the Phase 3 settle parameters | D:1187 [V] | live-only; offline the parameters are placeholders (§9) |

---

## 6. D§11.2 Q1–Q3 assessment

### 6.1 What is decided, and where

D§3 gives answers to Q1–Q3 and D§14 records "All four questions resolved" (D:1256). These are **[P] proposals in
a design review**, not applied schema. D§11.2 says so: "none applied; each needs separate authorisation" (D:1106),
and U12 (D:1191) makes *when* to apply them a governance decision, "recommended: before Phase 3". The Ph2 report
(§13 item 2), WIN §9 item 2 and U6M §9 item 3 all list the amendments as a Phase 3 prerequisite. [V]

The current dark schema (verified, `db.py:2538-2603`):

- states: `intent, sending, acknowledged, confirmed, rejected, unknown, adopted, abandoned, superseded, needs_review`;
- open set: `intent, sending, acknowledged, unknown, needs_review`; one unique partial index on `position_id`
  over that set, for all kinds together;
- CHECKs: `intent ⇒ sent_at NULL`; sent states ⇒ `sent_at NOT NULL`; create ⇒ `attempts ≤ 1`; modify/cancel ⇒
  `target_gtt_id`; ack/confirmed/adopted create ⇒ `result_gtt_id`; no rule for `abandoned`, `superseded`,
  `needs_review`.

### 6.2 Per-question assessment

| | **Q1 — `superseded`** | **Q2 — `abandoned`** | **Q3 — `needs_review` / open index** |
|---|---|---|---|
| **Repository question** | P1-Q1: "`superseded`: a state or a flag?" As a terminal state it leaves the open index while a resolution is still owed, so a second op could be claimed (Phase 1 report §8). | P1-Q2: "`abandoned` has two meanings with different consequences": "not sent" (auto re-create allowed) vs "absent after settle" (user decides); no column distinguishes them. | P1-Q3: the P§7.2 exit-claim predicate (`intent, sending, acknowledged, unknown`) and the index (adds `needs_review`) disagree; should `needs_review` block a manual exit? |
| **Current documented decision** [P] | D§3 Q1 (D:262-302): remove the state; overlay columns `superseded_at`, `superseded_by_kind` (`emergency_exit`, `exit`, `close`, `user`), `superseded_by_ref`; op leaves the open set only when its broker outcome resolves; atomic pre-send supersession via the `intent→sending` CAS predicates; resolution that yields a GTT id claims a `cancel` in the same transaction. | D§3 Q2 (D:314-340): `not_sent` (CHECK `sent_at IS NULL`; auto new create allowed unless superseded) and `absent_after_settle` (CHECK `sent_at NOT NULL AND complete_reads_after ≥ 2`, create only; never auto-retried; watched for late appearance → `LATE_GTT_APPEARED`). Settle counts complete reads ≥ 60 s apart, first after `max(sent_at+SETTLE, recovery start)`. | D§3 Q3 (D:350-389): `needs_review` stays open and blocks automated create/modify; does **not** block an exit claim in the DB predicate (the claim supersedes); clears only by an audited human resolution with a fresh complete read → `resolved_manual` + `protection_operation_resolutions` row; index partitioned into one-open-change (create/modify) and one-open-cancel. |
| **Unresolved choices** | (a) which actor kinds map to `superseded_by_kind` once Phase 6 wiring exists; (b) whether the overlay also needs an event type (`protection_superseded`, D:1152) now or in Phase 6; (c) P1-Q7: does `trade_tickets.protection_state` carry `CLOSED_CLEANUP_REQUIRED` (D refers to it, D:278, D:474, but it is not in `PROTECTION_STATES`, `db.py:2547-2552` [V]). | (a) concrete `PROTECTION_SETTLE_SECONDS` and `LATE_MATCH_WATCH_DAYS` (proposed 30, D:338) depend on P§10 items 9, 11, 12 [U] → config placeholders; (b) whether a suspend/resume between settle reads should reset `complete_reads_after` (§10 G4); (c) whether `absent_after_settle` CHECK should also require `kind = 'create'` (D:1145 says yes; D:319 omits it) — reconcile. | (a) the exact `outcome` enum of resolutions (D:1148 lists `linked_gtt, no_app_gtt, deleted_in_kite_verified, other`); (b) whether `resolved_manual` is allowed from `unknown` as well as `needs_review` (D:476 only `needs_review`); (c) the D§4.2 retry rule needs a modify `direction` column (D:1150); (d) which restriction reasons gate automated creates (see §12 DD-9). |
| **Required schema change** | 3 overlay columns + all-or-none CHECK (D:1146); remove `superseded` from the enum. | Remove `abandoned`; add two states + CHECKs (D:1143-1145). | Add `resolved_manual`, `resolution_id` FK + CHECK, new `protection_operation_resolutions` table (D:1147-1148); replace `idx_protection_ops_one_open` with `idx_protection_ops_one_open_change` and `idx_protection_ops_one_open_cancel` (D:366-373). |
| **Also in D§11.2** | `direction` column + retry CHECK (D:1150); `updated_at` (P1-Q9) and a `reason` CHECK once the list is final (P1-Q4) (D:1151); event types `protection_superseded`, `protection_resolved` (D:1152). | | |
| **Required test changes** | Replace the `superseded` cases in `test_dark_protection_schema.py:325,373`; add overlay CHECK tests; pre-send CAS race test (supersede vs `intent→sending`, both orders). | Replace `abandoned` cases (`:325,373,456-458,530`); CHECK tests for both new states; settle-rule tests on complete reads only (failed/partial reads increment nothing). | Index tests for the partition (cancel coexists with an unresolved create/modify; two open changes refused; two open cancels refused); resolution requires a fresh complete read; mutation checks (drop `needs_review` from the change index; merge the indexes). |
| **Classification** | Amendment: **BLOCKER — PHASE 3 ENGINEERING**. Sub-choices: **REQUIRED DESIGN DECISION**. Emergency Exit wiring: Phase 6, not Phase 3. | Amendment: **BLOCKER — PHASE 3 ENGINEERING**. Settle parameters: **REQUIRED DESIGN DECISION** (placeholders); their real values: **UNVERIFIED — EXTERNAL** (live-only). | Amendment: **BLOCKER — PHASE 3 ENGINEERING**. Sub-choices: **REQUIRED DESIGN DECISION**. |

### 6.3 The specific review points

| Point | Assessment |
|---|---|
| Operation state model | D§4.1 (D:418-437) gives 11 states (5 open, 6 terminal) with an overlay. Coherent [I]; not in the DDL [V]. |
| `superseded` semantics | Overlay, never a resolution (D:262-266). Safe because the op keeps its open-index slot until resolved [I]. |
| `not_sent` vs `absent_after_settle` | Provable vs inferred; distinguished by `sent_at`, enforced by CHECK (D:316-320). Provability rests on `sending` being committed before the call (P§5.2 step 6) — an engine invariant Phase 3 must test (crash boundaries) [I]. |
| `needs_review` resolution | Human-only, fresh complete read required, auditable row (D:357-358). Needs the resolutions table and a UI/route later (P§12 Phase 8); Phase 3 can implement the engine API and test it [I]. |
| Create/modify/cancel concurrency | Partitioned index + claim predicates (D:384-389): create/modify claim requires no open cancel, no exit in flight, no superseded open op, attribution allows, epoch current; cancel needs only epoch + partition. Delete vs in-flight modify converges to "gone" in either order (D:379-380) [I]. |
| Emergency Exit interaction | Supersede yes, take ownership no; cancels only *known* GTT ids; sells only under exclusive attribution; arms `POSSIBLE_DOUBLE_SELL` for an unknown create (D:286-294, D:356). Phase 3 can build the engine-side supersede API; `routes/kill_switch` wiring is Phase 6 (P:1198, P:1227). Item 5 (over-quantity sell rejection) is the unverified assumption behind proceeding in the exclusive case (P:1250-1251) [U]. |
| Late GTT appearance | While `unknown`: adopt only with exactly one fingerprint match and no other unlinked active SELL GTT; after `absent_after_settle`: never auto-adopt or auto-delete, raise `LATE_GTT_APPEARED` (D:329, D:477). |
| Late cancellation | A confirmed delete proves only absence at read time; a `triggered` read-back is a fire, not a cancel (D:777-780). Cancel retries are bounded and exposure-reducing (D:446). |
| Unique-index requirements | (1) the two partitioned open-op indexes; (2) `idx_trade_tickets_unique_gtt` (a GTT protects at most one ticket), which today is **skipped** when existing rows share a `kite_gtt_id` (`db.py:2819-2835` [V]). Whether the real DB has duplicates is **[U]** (the account DB was not opened). Once registered, a skip would be permanent because `migrate()` never re-runs a recorded version [I] — see §12 DD-6. |
| Does the current schema support the Phase 3 lifecycle safely? | **No.** With the current DDL, (a) a superseded op is terminal and leaves the index while its outcome is unknown (P1-Q1); (b) `abandoned` cannot tell "safe to re-create" from "inferred absent" (P1-Q2); (c) an Emergency Exit cancel cannot be claimed while a create/modify is unresolved (D:377-378). Each is a correctness defect for the engine [I]. |

---

## 7. Migration v12 assessment

### 7.1 Current registration (verified)

| Fact | Evidence |
|---|---|
| `MIGRATIONS` = v1…v11; v11 = `(11, "phase2_instance_coordination", _migration_v11_instance_coordination)`; `LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]` = 11 | `db.py:2245-2258` [V] |
| v11 is deliberately named outside `_mNNN_` so the `m011` prefix stays the Phase 1 dark marker | `db.py:2220-2221` [V]; Ph2 §3 |
| `_m011_protection_journal_dark` and `_r011_protection_journal_dark` exist and are **not** in `MIGRATIONS` | `db.py:2838-2868` [V]; `test_dark_protection_schema.py:192-194` asserts non-registration by identity [V] |
| The dark apply/rollback refuse the account DB and its backups, require no open transaction, and require the DB at `LATEST_SCHEMA_VERSION` | `db.py:2711-2737`, `:2871-2928` [V] |
| `migrate()` applies each pending version in its own `BEGIN IMMEDIATE` with FKs off around it, after one verified backup named for the **final** target (`pre_schema_v<target>_bak`) | `db.py:2358-2410` [V] |
| `_check_history` refuses a history newer than this code, or not a prefix of `MIGRATIONS` | `db.py:2278-2297` [V] |
| `rollback_schema_v11` refuses unless the history is exactly v1..v11 and the v11 objects are exactly present | `db.py:2459-2511` [V] |

### 7.2 What must be renumbered

- `_m011_protection_journal_dark` → `_m012_…`; `_r011_protection_journal_dark` → `_r012_…` (and their callers
  `apply_dark_protection_schema` / `rollback_dark_protection_schema`, `db.py:2889, 2925`).
- The pins: `test_dark_protection_schema.py:194` (identity set) and `:688` (regex `_m011_|_r011_` in the
  "workflows leave it unused" scan). [V]
- Nothing else uses the `m011` prefix: no registered migration carries it [V, §7.1].

### 7.3 Ordering, rollback and compatibility

| Question | Answer |
|---|---|
| Ordering preserved? | Yes, if v12 is appended as `(12, "<name>", _m012_…)`; the comment at `db.py:2243-2244` forbids renumbering released migrations [V]. |
| v10 DB → v11 → v12 safely? | `migrate()` would run both in one call, each in its own transaction, after a single backup named `pre_schema_v12_bak` [V]. **[I]** If v12 fails, v11 stays committed: the DB is at v11, which the Phase 2 build accepts; the backup name says v12 although it is the v10 image. A required offline test (below). |
| Can v12 run inside `migrate()`? | Phase 1 §7 warns `_m011` "must not call `_run_dark_step` itself" when registered. It does not: `_run_dark_step` is the caller, not called by `_m011` (`db.py:2889`) [V]. `_m011` re-checks `absent`, rebuilds `ticket_events`, verifies `present` — compatible with `migrate()`'s transaction [I]. |
| Older builds refuse v12 safely? | Yes: a Phase 2 build (LATEST 11) and a pre-Phase-2 build (LATEST 10) both raise `DATABASE_SCHEMA_UNSUPPORTED` on a newer history (`db.py:2283-2288`) [V]. Tested for v11 by running `init_db` with `MIGRATIONS[:10]` [T, Ph2 §3]. |
| Rollback compatibility | A v12 rollback must drop the v12 objects **and** delete `schema_version` row 12 (Phase 1 §7 notes `_r011` does not do the latter today [V]). `rollback_schema_v11` then works only after v12 is rolled back, because it demands history exactly v1..v11 (`db.py:2471-2477`) [V]. The dark rollback refuses while journal data exists (`db.py:2912-2923`) [V], so after Phase 3+ writes, rollback means restoring a backup — a governance consequence to state before registration. |
| Unique GTT index | If duplicates exist, v12 would record success without `idx_trade_tickets_unique_gtt` and never retry [I] (§12 DD-6). |
| Must a migration decision be final before Phase 3? | **Only the "dark vs registered" decision.** The amended DDL and renumbering are S1. Registration itself is not needed to build or test the dark engine [I]. |

### 7.4 Recommendation

Keep v12 **dark** through Phase 3 (apply only through the explicit function on scratch databases, refusing the
account DB as today). Register it only under a separate authorisation, with the operator backup, when the engine
is first wired (Phase 4/5). This keeps every Phase 3 test offline and leaves the real database untouched. [I]
→ **REQUIRED DESIGN DECISION** (§12 DD-2).

### 7.5 Required isolated migration tests (**REQUIRED OFFLINE TEST**)

All on `tmp_path` databases through the `env` fixture and guard:

1. Amended dark DDL: exact stored SQL, every CHECK (positive and negative rows), both partitioned indexes.
2. Apply → rollback → apply cycles; rollback byte-identical; refusal while holding data.
3. Chain v10 → v11 → v12 (when v12 is eventually registered): row-level preservation of every table, including
   `sqlite_sequence` and the `ticket_events` high-water mark.
4. Failure injection mid-v12 (exception, `KeyboardInterrupt`, `os._exit` child): DB left at v11, consistent.
5. Older-build refusal: `MIGRATIONS[:11]` and `[:10]` refuse a v12 DB.
6. Rollback ordering: `rollback_schema_v11` refuses on a v12 DB; v12 rollback then v11 rollback reaches v10.
7. Duplicate `kite_gtt_id` rows: behaviour chosen in DD-6 (skip + finding, or refuse).
8. Account-DB refusal for the renamed functions (Phase 1 test 12 equivalents).

---

## 8. U1 — DDPI/TPIN and GTT-triggered CNC SELL

| Question | What the repository establishes | Label |
|---|---|---|
| What authorisation may be required? | Only: "Sell GTTs on equity holdings are authorised through CDSL TPIN or DDPI (Zerodha support)" (`PROPOSAL_EXECUTION_GTT_AFTER_FILL.md:18`), restated at P:1156 as [REPO-DOC]. No other repository source mentions DDPI, TPIN or eDIS (grep) [V]. | [V] for what the docs say; behaviour [U] |
| DDPI vs TPIN vs other | P:1156 frames it as: unattended execution without DDPI? With TPIN only, is a same-day eDIS pre-authorisation needed? | [U] |
| At GTT creation, trigger time, or execution? | Not established anywhere. | [U] |
| If absent or expired? | Not established. P§13 risk 2: "broker-side protection may silently not work. This alone can block GO." | [U]; consequence [I] |
| Can a triggered GTT fail and leave the position exposed? | Plausibly yes [I]; Kite behaviour [U]. | |
| What can the app safely detect and communicate? | (a) A GTT read back as `triggered` whose linked order is rejected or absent — the existing scenario-T logic and Stage 1's "GTT triggered today whose order is missing" inconsistency (Ph2 §7 item 6; `startup_reconciliation.detect_inconsistencies`) [V]; (b) the position still open after a trigger → `PROTECTION_FAILED`/`NEEDS_REVIEW` per P's model [P]; (c) a user-declared DDPI status shown as a readiness disclosure [P]. The app **cannot** pre-verify DDPI/TPIN through any API it uses [I]. | |
| FakeKite | No DDPI/TPIN model (grep of `tests/fake_kite.py`) [V]. `trigger_gtt` already lets a test set the triggered order status (`fake_kite.py:160`) [V], so a "triggered → order REJECTED (authorisation)" scenario can be modelled. | model only, not evidence (Phase 0 §7 item 8) |

**Classification**

- Behaviour: **UNVERIFIED — EXTERNAL/BROKER EVIDENCE REQUIRED** (official docs; the user's own read-only check of
  the account's DDPI status in Kite, P:1156).
- Live: **BLOCKER — LIVE ACTIVATION ONLY**.
- Offline engineering: **NOT A BLOCKER**. A model test of "trigger fired, order rejected, position still open →
  surfaced, never treated as protected or closed" is a **REQUIRED OFFLINE TEST**.

---

## 9. P§10 items 3, 4, 5 and 9

| | **Item 3** | **Item 4** | **Item 5** | **Item 9** |
|---|---|---|---|---|
| **Exact issue / source** | Can a CNC SELL GTT be placed before the purchased shares are visible in holdings/positions (seconds after a fill)? (P:1153) | Does trigger and execution depend on holdings availability at trigger time (T0 vs T1 vs settled)? (P:1154) | A triggered GTT whose quantity exceeds the sellable quantity: rejected, partial, or a short sale (auction risk)? (P:1155) | `get_gtts()` read-after-write consistency and list completeness (pagination, non-active GTTs) (P:1159) |
| **Existing evidence** | None; [UNVERIFIED]. FakeKite does not model a placement refusal before visibility [I from grep]. | None; [UNVERIFIED]. FakeKite `trigger_gtt_against_holdings(policy=…)` (`fake_kite.py:199`) models it as a policy [V]. | [UNVERIFIED]; [INFERRED] RMS rejection likely for CNC, not assumed (P:1155). FakeKite policy `reject` default (`fake_kite.py:199`) [V]. | [UNVERIFIED]. FakeKite listing lag: "a new GTT is missing from the next N `get_gtts()` listings" (`fake_kite.py:73`) [V]. |
| **Status** | Open | Open | Open; "**never** tested live by design" (P:1155) | Open (also U8, D:1187) |
| **Missing evidence / decision** | Broker behaviour; resolution by supervised one-share validation (Phase 9). Decision: retry policy for a placement refused because shares are not yet visible (it is a definite rejection, so `rejected`; the next create is a new op after the pre-write check) [P]. | Official docs + validation. Decision: what the engine reports for a day-0 GTT whose protective value is unknown (disclose, not hide) [P]. | Official docs only. Decision: Emergency Exit exclusive-only selling relies on it (P:1250-1251). | Validation by repeated reads after create (Phase 9). Decision: `PROTECTION_SETTLE_SECONDS`, the ≥ 2 complete reads ≥ 60 s rule, `LATE_MATCH_WATCH_DAYS` as placeholders. |
| **Blocks** | Live activation | Live activation | Live activation | Live activation; Phase 3 only through parameters (placeholders suffice) |
| **Safest resolution path** | Engine treats "not yet visible" as `rejected`/`not_sent` and keeps the position `PROTECTION_PENDING` with a visible gap; Phase 9 supervised trade decides. | Disclose; never count a day-0 GTT as proven protection until item 4 is answered. | Never test live; obtain official documentation; keep the exclusive-only Emergency Exit rule and the `POSSIBLE_DOUBLE_SELL` watch. | Settle counts **complete reads**, not time (D:334-335); absence after settle is inference with a watch (D§3 Q2); never auto-retry a create. |
| **Classification** | **UNVERIFIED — EXTERNAL**; **BLOCKER — LIVE ACTIVATION ONLY** | same | same | same; engine parameters: **REQUIRED DESIGN DECISION** |

FakeKite scenarios for all four exist or are cheap to add; they are **REQUIRED OFFLINE TEST**s of the engine's
fail-closed behaviour, not evidence of Kite behaviour.

---

## 10. Phase 2 residual architecture risks

| # | Brief item | Finding | Label | Classification |
|---|---|---|---|---|
| 1 | Instance lock and epoch fencing | OS lock on `<DB>.instance.lock`, takeover epoch, `fenced_transaction` re-checks `(epoch, instance_uuid)` under `BEGIN IMMEDIATE` and drops authority on mismatch (`instance_lock.py:700-729`). Tests L1–L6, E1–E6, S1, S2, C1, C2 and 8 mutation checks reported passing. | [V] code; [T] Ph2 §9.2, §9.5 | **NOT A BLOCKER** |
| 2 | 15 s heartbeat, suspend/resume detection | `run_heartbeat_loop`: `asyncio.sleep(HEARTBEAT_SECONDS)` then `heartbeat_once` (`:802-811`); resume if either the wall or monotonic gap > 3 × 15 s (`:745-755`). Real Modern Standby and Hibernate detected. | [V]; [T] WIN §3 | **NOT A BLOCKER** |
| 3 | Latency window resume → detection | Up to one heartbeat interval plus scheduling delay; not measured (WIN §8.3). | [V] doc; magnitude [U] | **SUPPLEMENTARY VALIDATION** (measure) |
| 4 | Sensitive operations before resume detection | **Yes, possible.** The five loops check only `loop_may_run` → `authority_ok()` (`instance_lock.py:666-673`; `order_monitor.py:104`, `trailing_engine.py:39`, `reconciliation.py:113`, `automation.py:122`, `data_jobs.py:69`) [V]. Write routes check only authority (`:635-640`) [V]. After wake, the loops' timers and the heartbeat's timer can all be due together and run in any order [I], so a loop cycle or a route can act before `RESUMED_AFTER_SUSPEND` is set. **G1** below. | [V] + [I] | **REQUIRED DESIGN DECISION** + **REQUIRED OFFLINE TEST** (Phase 3) |
| 5 | Resume invalidates observations and forces Stage 1 | Yes for GTT observations (`gtt_observations.reset()`, `:757-762`), the persisted `RESUMED_AFTER_SUSPEND` and a Stage 1 request (`startup_reconciliation.note_resume`, `:295-313`), and `derive_restriction` demands a Stage 1 passed after the last resume (`:213-227`) [V]. **Not** reset: reconciliation's cross-cycle debounce memory (`_price_seen`, `_reverse_seen`, `last_cycle`); `reset_cycle_memory` (`reconciliation.py:100`) has no production caller [V]. So one pre-suspend and one post-resume sighting can confirm a two-cycle finding [I]. **G4**. Broker-side Stage 1 after a real resume never executed (WIN §8.4). | [V], [I], [U] | G4: **REQUIRED DESIGN DECISION** (minor); broker part: **UNVERIFIED — EXTERNAL** |
| 6 | Startup RESTRICTED | Persisted in the takeover transaction before the loops; mutation M6 caught. | [V] Ph2 §4; [T] Ph2 §9.5 | **NOT A BLOCKER** |
| 7 | Read-only Stage 1 | Stage 1 makes no broker write; incomplete reads never mean "absent" (R1–R5). **But it does not read the protection journal** (Ph2 §7 item 1: "The dark protection journal is not read"). Phase 3 must add D§8.3 step 1: each `intent` with `sent_at IS NULL` → `not_sent` (fenced) and each older-epoch `sending` → `unknown`; and the reasons `PROTECTION_GAP`, `LATE_GTT_APPEARED`, open ops in `UNRESOLVED_OPERATION` (Ph2 §7: "never set in Phase 2"). **G3**. | [V], [T] | **BLOCKER — PHASE 3 ENGINEERING** scope item (inside Phase 3, before the engine is considered complete) |
| 8 | Background loops initiating protection writes before reconciliation | **Yes, by design** (D§8.2, D:878-888): loops start before Stage 1 so that exposure-reducing upkeep continues. Today's legacy upkeep (fill-following `sync_protection`, trailing ratchet) runs while RESTRICTED. In Phase 3 the engine is dark, so this is unchanged; the engine's **own** creates and resize-ups must gate on "Stage 1 passed in this epoch after the last login and resume" (D§8.4). When the engine replaces the legacy paths (Phase 5), the legacy pre-Stage-1 writes must become engine ops under that gate. | [V] + [P] | **REQUIRED DESIGN DECISION** (DD-9) |
| 9 | Kite SDK redirect mitigation | `max_redirects = 0` on every factory client; 136 tests incl. positive controls; mutation caught (74 failures). | [V] U6M §1 (code cited there); [T] U6M §3-4 | **NOT A BLOCKER** (client-side replay closed) |
| 10 | Cannot cancel an already-sent delayed request | Inherent [I] (D:775). Covered by design only: write-ahead `sending`, never re-send a create or resize-up, successor treats older-epoch `sending` as `unknown`, settle in complete reads, adoption precondition, `LATE_GTT_APPEARED` (D:775). None of this exists yet (no journal code). | [P] | Phase 3 scope; **REQUIRED OFFLINE TEST** (F1/F2, crash boundaries) |
| 11 | Local DB writes after authority is lost | (a) Legacy cycles past their authority check finish their local writes (D§7.3; Ph2 §5, §12) — disclosed; the U10 `sqlite3` authorizer was not built. (b) **G2:** a Phase 3 op that sent a request and then loses authority cannot record the response: `fenced_transaction` raises `FencingError` (`:717-721`), so a returned GTT id would exist only in memory. The successor must then adopt by fingerprint. Phase 3 should write the broker result to the structured log (and keep it out of the DB) so a human can reconcile it [P]. (c) `broker_write` checks only the in-memory role, not the DB epoch (`:649-663`); the window is small if the `sending` CAS is fenced and immediately precedes the call [I]. | [V], [I], [P] | (a) **NOT A BLOCKER** (disclosed, lock-bypass only); (b), (c) **REQUIRED DESIGN DECISION** + **REQUIRED OFFLINE TEST** |

### Architectural gaps Phase 3 must address

- **G1 — resume-latency claim guard.** Every engine claim that can increase sell exposure (create, resize-up)
  performs, inside its fenced transaction, a synchronous check of both clocks against the last successful
  heartbeat (state already held in `instance_lock`), treating a gap > threshold as a resume (refuse, and trigger
  the resume path), plus the "Stage 1 passed after the last resume" condition. Exposure-reducing classes (cancel,
  modify-down) stay allowed, matching "RESTRICTED never blocks exits, cancellations, Emergency Exit or protection
  upkeep" (D:1243). Testable offline with injected clocks.
- **G2 — response after authority loss** (row 11b).
- **G3 — journal-aware Stage 1** (row 7).
- **G4 — cross-cycle memory across a resume** (row 5): decide whether resume resets it.
- **G5 — U6M §8 item 6** (repeated `modify_gtt`/`delete_gtt` semantics unknown): D§4.2's retries must be correct
  whether a repeat is a no-op or an error; FakeKite should model both.
- **G6 — test infrastructure.** The shared-state multi-process FakeKite and the day-rollover harness, deferred
  since Phase 0 (Phase 0 §9; D:1049-1053), are not present [V: no match for rollover in `tests/`]; **D§10.2 F2**
  ("ambiguous outcomes across the fence and the epoch change: the successor never re-sends", D:1089) is not
  implemented — `tests/test_startup_reconciliation.py` names F1/F2 in its docstring but has only
  `test_F1_an_unknown_entry_outcome_is_never_resent_by_stage1` (`:393`) [V]. The `os._exit` crash harness exists
  (Ph2 C1) [T]. → **REQUIRED OFFLINE TEST**.

**Passing tests do not prove broker-side behaviour.** Every FakeKite result above is a model (Phase 0 §7 item 8).

---

## 11. Windows validation follow-ups

| Item | Accepted result | Caveat | Classification |
|---|---|---|---|
| L3 subst drive | PASS (runner + 50/50 supplementary: subst, case, junction, subst + junction, release) | one machine, not elevated | **NOT A BLOCKER** [T] WIN §2 |
| L6 network share | PASS over loopback SMB (`\\localhost\C$`, `\\127.0.0.1\C$`, hostname, mapped drive; all `DRIVE_REMOTE`) | **a share on another host / NAS not tested**; NAS, DFS, WebDAV, cloud-sync drives untested (WIN §4.4) | remote re-run: **SUPPLEMENTARY VALIDATION** |
| R8 suspend/resume | PASS: real Modern Standby (514 s) and Hibernate (612 s); lock held throughout; authority kept; observations reset; Stage 1 requested | Stage 1 after resume **not executed** (needs a broker read); lid-close, critical-battery hibernate, S3 untested (WIN §8.1) | **NOT A BLOCKER**; broker part **UNVERIFIED — EXTERNAL** |
| Broker reconciliation after resume | not tested | needs a broker | **UNVERIFIED — EXTERNAL/BROKER EVIDENCE REQUIRED** (Phase 9) |
| **R8 runner defect** | The runner printed `PASS` for a 73.3 s hibernate (WIN §3.3) | see below | **SUPPLEMENTARY VALIDATION** (fix before R8 is reused as evidence) |
| `WIN-INT PENDING` skip placeholders | still read PENDING (reported at `tests/test_instance_lock.py:194, 233, 674`, WIN §8.7) | documentation drift | **SUPPLEMENTARY VALIDATION** |

### 11.1 R8 runner: verified defect and recommended correction (not applied)

**Defect [V]** (`tests/winint_phase2.py:103-118`): the loop sleeps `HEARTBEAT_SECONDS`, calls
`instance_lock.heartbeat_once()`, and on `result["resumed"]` decides `ok = RESUMED_AFTER_SUSPEND set and probe
BUSY and result["ok"]`. It never measures the suspend duration, and `heartbeat_once`'s result carries no gap
(`instance_lock.py:739`). Any gap above 45 s (3 × 15 s) passes, so the 73 s attempt passed despite the
documented "at least 2 minutes" (`winint_phase2.py:8, 100`).

**Recommended correction (runner-only; production `heartbeat_once` unchanged):**

1. In `r8_sleep`, record `time.time()` and `time.monotonic()` immediately after each `heartbeat_once()` returns.
2. On the resumed heartbeat, compute `gap_wall` and `gap_mono` from the previous recorded pair.
3. Because the gap includes up to one heartbeat interval of awake time, use the conservative lower bound
   `suspended_at_least = gap_wall − HEARTBEAT_SECONDS` and require `suspended_at_least ≥ 120`
   (a named constant, e.g. `R8_MIN_SUSPEND_SECONDS = 120`). With the WIN data: 73.3 − 15 = 58.3 → **FAIL**;
   516.8 − 15 and 612.3 − 15 → PASS.
4. On a too-short suspend, print `FAIL R8 suspend too short (…s < 120 s): repeat the procedure` and exit
   non-zero, after the same `finish_shutdown` cleanup.
5. Print `gap_wall`, `gap_mono` and the bound on both the PASS and FAIL lines, so the log is self-evidencing.
6. Add an offline unit test that drives the decision function with injected clocks (gaps of 73 s, 134 s, 135 s,
   514 s) without sleeping. The wall clock is used for the bound because on other hardware the monotonic clock
   might not advance across sleep (U3 was answered only for this laptop, WIN §3.4).

---

## 12. Required decisions before implementation

All **REQUIRED DESIGN DECISION**, owned by the user. Recommendations are [P].

| # | Decision | Recommendation |
|---|---|---|
| DD-1 | Adopt D§3/§11.2 Q1–Q3 as the amended dark DDL (U12) | Yes, as the first Phase 3 step (S1) |
| DD-2 | v12: dark through Phase 3, or registered | Dark; registration is a separate gate with operator backup (§7.4) |
| DD-3 | `reason` value list and CHECK (P1-Q4) | Fix the list (e.g. `fill`, `exit_fill`, `trailing`, `cleanup`, `user_recreate`, `supersede_cleanup`) and add the CHECK now, while the table is empty |
| DD-4 | Modify `direction` column and the retry CHECK (D:1150, D§4.2) | Add; `unknown → sending` only for cancel and modify-down |
| DD-5 | Transition enforcement: CAS-only or triggers (P1-Q6); op↔position `account_id`/`mode` consistency (P1-Q5) | CAS predicates under `fenced_transaction` (D:488-489) plus an engine invariant test; triggers optional |
| DD-6 | Duplicate `kite_gtt_id` rows when the unique index is created (Phase 1 D4) | For the dark phase keep "skip + report"; before registration decide between refusing the migration and an URGENT finding plus a startup re-check |
| DD-7 | `protection_state` values: include `CLOSED_CLEANUP_REQUIRED` or a separate column (P1-Q7); `updated_at` (P1-Q9); the two new event types | Decide before S1 so `ticket_events` is rebuilt once |
| DD-8 | Settle and watch parameters (`PROTECTION_SETTLE_SECONDS`, complete-read count and spacing, `LATE_MATCH_WATCH_DAYS`, `PREWRITE_MAX_AGE_SECONDS`, `FILL_VISIBILITY_TOLERANCE_SECONDS`) | Config placeholders at the D values, marked pending P§10 items 9–12 |
| DD-9 | Which restriction reasons gate automated creates/resize-ups. `derive_restriction` adds `UNRESOLVED_OPERATION` for any open op (`startup_reconciliation.py:235-236`; Ph2 §7), so "NORMAL" would block the engine's own recovery | A dedicated predicate: own epoch, Stage 1 passed after last login and resume, fresh complete read, no `INSTANCE_*`/`HEARTBEAT_STALE`/`RESUMED_AFTER_SUSPEND`/`BROKER_*`/`LATE_GTT_APPEARED` on the account, attribution allows (D:384-389) |
| DD-10 | G1 resume claim guard; G2 log-only response recording after authority loss; G4 reset cross-cycle memory on resume | Adopt all three in Phase 3 |
| DD-11 | Phase 3 "dry run" meaning: tests only, or also dry-run tickets in the running app | Tests only (in-app dry run needs the table on the real DB, i.e. DD-2 registration) |

---

## 13. Proposed safe Phase 3 implementation sequence (not implemented)

Every stage: FakeKite and `tmp_path` databases only; the Phase 0 guard in every process (children call
`install()`/`neutralise_dotenv()` first); `LIVE_ENTRIES_ENABLED = False`; PK-25 13/13; `execution.py` identical to
HEAD; no `monkeypatch.undo()` inside `env` tests (Phase 1 §6.4).

| Stage | Scope | Depends on | Exit gate |
|---|---|---|---|
| **G0** Decisions | DD-1…DD-11 recorded by the user | — | written decisions |
| **S1** Schema amendments | Amend the dark DDL per D§11.2 + DD-3/4/7; renumber `_m011_*`/`_r011_*` → `_m012_*`/`_r012_*`; update `test_dark_protection_schema.py` pins; **not registered** | G0 | focused suite green; Phase 1 mutation checks re-run plus new ones (drop `needs_review` from the change index; merge the partitioned indexes; allow `not_sent` with `sent_at`) |
| **S2** v12 planning and isolated tests | Write the v12 down-migration (drops objects and row 12; refuses with data); the §7.5 test list on scratch DBs; a registration runbook draft (not executed) | S1 | §7.5 tests green; no change to `MIGRATIONS` |
| **S3** Harness | Shared-state multi-process FakeKite; day rollover; clock injection for resume; FakeKite models for DDPI-rejected trigger, repeated modify/delete (both semantics), placement refusal before visibility | S1 | harness self-tests; guard summary `unexpected 0` |
| **S4** State machine | `backend/protection.py` (dark): claims, `intent → sending` CAS with supersede and exit predicates, D§4.3 transitions as fenced CAS, retry classes D§4.2, paper parity | S1, S3 | P§9.3 A–M equivalents; illegal transitions refused (DD-5) |
| **S5** Broker ambiguity and late response | write-ahead `sending`; ambiguous → `unknown`; never re-send create/resize-up; G2 logging after authority loss; F1 | S4 | J* tests; crash boundaries "before/after the broker call" |
| **S6** GTT observation and reconciliation | read-back; adoption (exactly one match, no other unlinked SELL GTT); settle on complete reads; `needs_review` and resolutions with a fresh read; `LATE_GTT_APPEARED` watch | S4, S5 | I* tests; listing-lag and malformed-read cases never conclude "absent" |
| **S7** Emergency Exit and cancellation (engine side) | supersede overlay API; cancel claims for known ids; resolution-time cancel; `POSSIBLE_DOUBLE_SELL` arming; `triggered` read-back as a fire. **No `kill_switch` wiring** (Phase 6) | S4, S6 | supersede vs send race, both orders; cancel coexisting with an unresolved create |
| **S8** Restart, suspend, fencing | journal-aware Stage 1 (G3); G1 resume guard; older-epoch `sending` → `unknown`; DD-9 predicate; G4 | S4–S6 | C1-style crash boundaries 4–9; R8-style clock-jump tests; E2-style epoch change with an in-flight op |
| **S9** Fault injection and concurrency | `accept_then_raise`, `SimulatedCrash`, delays past timeout, multi-process races on one tmp DB (P1/S1 style), **F2** across the fence and epoch change | S3–S8 | the full P§9.3 matrix for Phase 3 (A–M, I*, J*, crash boundaries 4–9) |
| **S10** Regression and safety audit | full suite (the standing command, the two approved DMB exclusions only); mutation checks for every new guard; integrity checks as in Ph2 §14 | S9 | 0 failures; all mutations caught; integrity table |

After S10, these remain **separate authorisations**: v12 registration (with the operator backup), Phase 4
(`execution.py`), Phases 5–8, Phase 9 (any broker contact), Phase 10 (the gate).

---

## 14. Items that must remain prohibited

1. Changing `LIVE_ENTRIES_ENABLED` (stays `False`).
2. Editing `backend/execution.py` or any of the 13 PK-25 frozen files (Phase 4 needs its own authorisation and a
   supersede record).
3. Registering v12 in `MIGRATIONS`, running any migration on the real account database, or opening, copying or
   connecting to the account database, its backups, `.env*`, `backend/ui_prefs.json` or credentials.
4. Any broker contact, including read-only reads and any Kite MCP tool (Phase 9 only, separately authorised).
5. Wiring the engine into `order_monitor`, `trailing_engine`, `routes/kill_switch`, `routes/tickets` or any live
   path (Phases 4–6).
6. Any automatic retry of a create or a resize-up; any re-send of an op whose outcome may have been accepted.
7. Any journal write outside `instance_lock.fenced_transaction`; any broker write that bypasses the fence.
8. Tests or child processes without the Phase 0 guard; `monkeypatch.undo()` inside `env` tests.
9. Re-enabling HTTP redirects for writes (U6M §5: any read-side relaxation must not reopen writes).
10. Starting the real application before the operator backup (Ph2 §11).
11. Staging, committing, pushing, stashing, resetting, cleaning or switching branches without an explicit request.

---

## 15. Final integrity checks

Run after this report was written (sections 0–14), before this section was filled in. All checks are read-only;
git ran with `--no-optional-locks`.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged [V] |
| 2 | Worktree inventory | `diff status_before.txt status_after.txt` (both `git status --porcelain=v1 --untracked-files=all`) shows exactly one added line, `?? docs/PHASE_3_READINESS_REVIEW.md`. Every other entry (72 ` M`, 27 `??`) is unchanged [V] |
| 3 | Pre-existing content | `git hash-object` of all 99 pre-existing entries is identical before and after [V]. No tracked source, config, test or doc was modified |
| 4 | New files | only this report. Scratch files (`status_*.txt`, `dirty_hashes_*.txt`, `pk25.py`) are in the session scratchpad, outside the repository [V] |
| 5 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) [V] |
| 6 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`), same as the start [V] |
| 7 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, read by grep, not edited) [V] |
| 8 | Staged / committed / stashed | nothing staged (`git diff --cached --name-only` empty), no commit, no push, `git stash list` empty; no reset, clean, checkout or branch switch [V] |
| 9 | Broker / network | none. No Kite MCP tool, no broker API, no HTTP request, no application start, no test run [V: session command log] |
| 10 | `.env`, account DB, backups, `ui_prefs.json`, credentials | not opened, read, copied or stat'ed; appeared only as names in `git status` output if at all. Evidence is the session command log; no size/mtime comparison was taken, to avoid touching protected paths [V for "not opened"; a stat-level before/after is **not performed**] |
| 11 | Migrations | none run on any database [V] |

**Phase 3 has not been started.** Implementation waits for explicit authorisation of the §12 decisions and the
§13 sequence.
