# Phase 3 design decisions: protection engine (dark) and the gate to live activation

**Status: DESIGN AND DOCUMENTATION ONLY. Every decision below is SELECTED and awaits the user's approval.**

**Revision 2 (2026-11-09): corrections following review.** See [Revision history](#revision-history). Text changed in
revision 2 is tagged **[R2-n]**. Revision 1 text that no longer holds is kept visible, ~~struck through~~, next to its
replacement. Revision 1 text that still holds is unchanged.

**Revision 3 (2026-11-09): final corrections R3.** Three review items: DX classification precedence for
`caps_sell_exposure` (**[R3-1]**), the complete Emergency Exit G_max membership, deduplication and aggregate sell
quantity (**[R3-2]**), and the Emergency Exit grace-period semantics (**[R3-3]**). Revision 2 text that no longer holds
is ~~struck through~~ and labelled "superseded by R3-n". No active table or summary relies on struck text.

**Revision 4 (2026-11-09): S_agg clarification R4.** One correction (**[R4]**): how the Emergency Exit aggregate sell
quantity S_agg counts GTTs with several SELL legs, unresolved modifications, unresolved creates and cancels, and
several outstanding triggered SELL orders. The sale gate itself (𝒢 established, G_max = 0, S_agg = 0) is unchanged.
Revision 3 text that no longer holds is ~~struck through~~ and labelled "superseded by R4". No active table or summary
relies on struck text.

**Revision 5 (2026-11-09): final two design clarifications R5.** Two corrections, both inside M-7:

- **[R5-1]** An id-less CREATE (B-24) whose resulting GTTs cannot all be identified has an UNKNOWN worst case, so
  S_agg is UNKNOWN. Its payload SELL quantity is no longer presented as a lower bound.
- **[R5-2]** R3-3 condition (e) and grace row G6: a triggered multi-leg GTT leaves 𝒢 only when every triggered SELL
  order and every SELL leg of the GTT is proven unable to execute. One terminal order is never enough.

The sale gate (𝒢 established, G_max = 0, S_agg = 0, no override) is unchanged. Revision 4 and earlier text that no
longer holds is ~~struck through~~ and labelled "superseded by R5". No active table or summary relies on struck text.

**Revision 6 (2026-11-09): triggered-order terminology R6.** One correction (**[R6]**), inside M-7. It separates two
sets that rev. 5 described with the same words:

- **T(m)**, the **complete triggered-order set** of GTT member m: every triggered SELL order associated with m, whether
  open, partially filled, terminal, cancelled or rejected, plus every order the evidence indicates should exist but
  that is not yet visible in the broker listing.
- **O(m)**, the **outstanding triggered-order subset** (the R4 meaning, unchanged): the orders in T(m) that could
  still execute.

Condition (e) and grace row G6 now evaluate the completeness of **T(m)**. O(m) is used only for outstanding sell
exposure under R4. The R4 S_agg formula, the sale gate and the no-override rule are unchanged. Revision 5 and earlier
text that no longer holds is ~~struck through~~ and labelled "superseded by R6". No active table or summary relies on
struck text.

- None takes effect, and Phase 3 engineering does not start, until the user approves this document (gate
  **G0** of `docs/PHASE_3_READINESS_REVIEW.md` §13).
- This document changes no code, test, schema, migration registration, configuration or database.
- `backend/execution.py` and the 13 PK-25 frozen files are untouched.
- `backend/live_entry_gate.LIVE_ENTRIES_ENABLED` **stays `False`**.
- No broker connection, network call, real-database access, application start or test run was made.

### Citations

| Short form | Document |
|---|---|
| **RR§n** / RR:line | `docs/PHASE_3_READINESS_REVIEW.md` |
| **D§n** / D:line | `docs/PHASE_2_DESIGN_REVIEW.md` |
| **P§n** / P:line | `docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md` |
| **P1-Qn** | `docs/PHASE_1_DARK_SCHEMA_REPORT.md` §8 |
| **Ph2§n** | `docs/PHASE_2_IMPLEMENTATION_REPORT.md` |
| **U6M§n** | `docs/U6_MITIGATION_REPORT.md` |
| **DD-n** | the decisions listed in RR§12 |
| **M-n** | the ten mandatory decisions of this brief (§3) |

### Evidence labels (as in RR)

| Label | Meaning |
|---|---|
| **[V]** | Verified from the repository source or docs in this session, with a file reference |
| **[T]** | Test evidence reported in an existing report (no test was run in this session) |
| **[I]** | Inference: reasoned, not proven |
| **[U]** | Unknown, or dependent on broker or external behaviour |
| **[S]** | Selected design decision in this document (awaiting approval) |
| **[R2-n]** | Changed in revision 2; `n` indexes the [Revision history](#revision-history) |
| **[R3-n]** | Changed in revision 3; `n` indexes the [Revision history](#revision-history) |
| **[R4]** | Changed in revision 4 (one correction; see the [Revision history](#revision-history)) |
| **[R5-n]** | Changed in revision 5; `n` is 1 or 2 (see the [Revision history](#revision-history)) |
| **[R6]** | Changed in revision 6 (one correction; see the [Revision history](#revision-history)) |
| **UNVERIFIED** | A fact this revision could not establish from source evidence; stated with its fail-closed posture, never assumed |

### Recorded starting state (2026-11-09)

| Item | Value | How established |
|---|---|---|
| Repository / branch | `D:/Claude_WS/kite-terminal` / `feature/signal-detail-and-strategy-revision` (expected) | `git branch --show-current` [V] |
| HEAD | `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (expected) | `git rev-parse HEAD` [V] |
| Working tree | 100 entries: the 99 recorded in RR§3.1, plus `?? docs/PHASE_3_READINESS_REVIEW.md`. Nothing staged. Snapshot and per-file `git hash-object` saved to the session scratchpad (`status_before.txt`, `hashes_before.txt`) | `git --no-optional-locks status --porcelain=v1 --untracked-files=all` [V] |
| `backend/execution.py` | identical to HEAD | `git diff --quiet HEAD -- backend/execution.py` [V] |
| PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, `research/pk25/validation_manifest.json` → `model_freeze`) | scratchpad `pk25.py`, read-only, no `backend` import [V] |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) | grep [V] |
| **Revision 2 starting state** | Same branch and HEAD. **101 entries**: the 100 above plus `?? docs/PHASE_3_DESIGN_DECISIONS.md` (rev. 1). Nothing staged. `execution.py` identical to HEAD. PK-25 13 / 13. `LIVE_ENTRIES_ENABLED = False`. Snapshot (`status_before.txt`), sha256 of the 100 other entries (`hashes_before.txt`) and a copy of rev. 1 (`PHASE_3_DESIGN_DECISIONS.orig.md`) are in the session scratchpad | `git rev-parse`, `git status --porcelain`, `sha256sum`, scratchpad `pk25.py` (read-only) [V] |
| **Revision 3 starting state** | Same branch and HEAD. **101 entries**, identical to the revision 2 inventory (this document is still `??`). Nothing staged. `execution.py` identical to HEAD. PK-25 13 / 13. `LIVE_ENTRIES_ENABLED = False`. A new session scratchpad holds `status_before.txt`, the sha256 of the 100 other entries (`hashes_before.txt`), a copy of rev. 2 (`PHASE_3_DESIGN_DECISIONS.rev2.md`) and a new read-only `pk25.py` | `git rev-parse`, `git --no-optional-locks status --porcelain=v1 --untracked-files=all`, `sha256sum`, `pk25.py` (no `backend` import) [V] |
| **Revision 4 starting state** | Same branch and HEAD. **101 entries**, identical to the revision 3 inventory (this document is still `??`). Nothing staged. `execution.py` identical to HEAD. PK-25 13 / 13. `LIVE_ENTRIES_ENABLED = False`. A new session scratchpad holds `status_before.txt`, the sha256 of the 100 other entries (`hashes_before.txt`), a copy of rev. 3 (`PHASE_3_DESIGN_DECISIONS.rev3.md`) and a new read-only `pk25.py` | `git rev-parse`, `git --no-optional-locks status --porcelain=v1 --untracked-files=all`, `sha256sum`, `pk25.py` (no `backend` import) [V] |
| **Revision 5 starting state** | Same branch and HEAD. **101 entries**, identical to the revision 4 inventory (this document is still `??`). Nothing staged; `git stash list` empty. `execution.py` identical to HEAD. PK-25 13 / 13. `LIVE_ENTRIES_ENABLED = False`. A new session scratchpad holds `status_before.txt`, the sha256 of the 100 other entries (`hashes_before.txt`), a copy of rev. 4 (`PHASE_3_DESIGN_DECISIONS.rev4.md`) and a new read-only `pk25.py` | `git rev-parse`, `git --no-optional-locks status --porcelain=v1 --untracked-files=all`, `sha256sum`, `pk25.py` (no `backend` import) [V] |
| **Revision 6 starting state** | Same branch and HEAD. **101 entries**, identical to the revision 5 inventory (this document is still `??`). Nothing staged; `git stash list` empty. `execution.py` identical to HEAD (`git hash-object` equals the `HEAD:backend/execution.py` blob; the working copy is CRLF under `core.autocrlf=true`). PK-25 13 / 13. `LIVE_ENTRIES_ENABLED = False`. A new session scratchpad holds `status_before.txt`, the sha256 of the 100 other entries (`hashes_before.txt`), a copy of rev. 5 (`PHASE_3_DESIGN_DECISIONS.rev5.md`) and a new read-only `pk25.py` | `git rev-parse`, `git --no-optional-locks status --porcelain=v1 --untracked-files=all`, `git hash-object`, `sha256sum`, `pk25.py` (no `backend` import) [V] |

### Source read for this document

All reads were read-only.

- **Docs:** RR (all of it); D (all of it); Ph2 (all of it); the Phase 1 report (all of it); P§3.3–3.7, §5.3–5.5,
  §7.2–7.3, §10; U6M§8–9.
- **Code:**
  - `backend/db.py`: `MIGRATIONS` `:2245-2258`; the dark section `:2514-2928`, including `_ensure_unique_gtt_index`
    `:2819` and `_m011`/`_r011` `:2838-2868`;
  - `backend/instance_lock.py`: `authority_ok` `:611`, `broker_write` `:649`, `loop_may_run` `:666`,
    `fenced_transaction` `:700`, `heartbeat_once` `:734`, `run_heartbeat_loop` `:802`;
  - `backend/startup_reconciliation.py`: `derive_restriction`, `note_resume`;
  - `backend/gtt_cancellation.py` (all of it);
  - `backend/reconciliation.py` `:75-103` (`reset_cycle_memory`);
  - the `trade_tickets` DDL (`db.py:75-110`: `account_id NOT NULL`, `mode` nullable, `kite_gtt_id`, `closed_at`);
  - `backend/config.py`: `HEARTBEAT_SECONDS=15`, `INSTANCE_LEASE_SECONDS=60`, `FILL_VISIBILITY_TOLERANCE_SECONDS=60`,
    `GTT_OBSERVATION_MAX_AGE_SECONDS`.
- **Code read for revision 2** (read-only; no execution, no import):
  - `backend/db.py`: `trade_tickets` DDL `:75-111` and its rebuild copies `:590`, `:1452`; `execution_settings`
    `:143`; `broker_reads` `:2127-2142`; dark state lists `:2540-2556`; dark `mode` CHECK `:2574`;
  - `backend/routes/tickets.py`: ticket inserts `:239`, `:350-392`; `_claim_mode` / `_require_valid_mode`
    `:719-764`; `_exit_claim_condition` `:820-826`; the claim `_transition(…, mode=mode)` `:1160-1170`;
  - `backend/paper_broker.py` `:100-135` (`VALID_MODES`, `is_valid_mode`); `backend/strategy_engine.py` `:386`,
    `:477`; `backend/trailing_engine.py` `:669`; `backend/routes/kill_switch.py` `:60-80`, `:160-232`, `:573`;
    `backend/risk_engine.py` `:396-424`; `backend/execution.py` `:21-50`, `:333` (read only, not modified);
  - `backend/instance_lock.py` `:139`, `:600-830`; `backend/startup_reconciliation.py` `note_resume` `:295-313`,
    `read_broker` `:321-372`, `:585`, `:628-629`; `backend/reconciliation.py` `:75-103`, `:187-211`;
    `backend/gtt_observations.py` `_lock` `:22`, `reset` `:125-127`;
  - docs: D§4.2 (D:436-446), D§6.3 (D:648-676), P§3.3 (P:251-265).
- **Code read for revision 3** (read-only; no execution, no import):
  - `docs/PHASE_2_DESIGN_REVIEW.md` D§6.3–§6.4 (D:648-686): attribution states; `FILL_NOT_YET_VISIBLE` → `unknown`;
    `SHORTFALL` → `broken` only on two consecutive complete reads; resize-down to `min(app open, broker_total −
    baseline)` under `broken`;
  - `backend/db.py` `:2540-2552` (`PROTECTION_OPEN_OPERATION_STATES`, `PROTECTION_STATES`, including
    `PROTECTION_NEEDS_REVIEW`);
  - `backend/routes/kill_switch.py` `:91-140` (one result per position), `:200-216` (E3 is evaluated **per
    instrument**, summing every open live app position; "shares are fungible"), `:273-283` (`_app_total`), `:555-562`
    (EE ticket sized to the open quantity).
- **Code read for revision 4** (read-only; no execution, no import):
  - `backend/db.py` `:2575-2582`, the dark `protection_operations` DDL: `requested_quantity` (`:2580`, one integer)
    and `request_payload` (`:2582`, JSON), matching §4.1 of this document;
  - `backend/db.py` `:1556-1565`, the paper GTT table: `trigger_type IN ('single', 'two-leg')` (`:1560`) with a
    separate `legs` column (`:1565`). A GTT request can therefore carry more than one leg, so one integer cannot
    describe its sell exposure.
- **Revision 5** cites no new code. It re-reads this document only. The integrity checks also read
  `research/pk25/validation_manifest.json` and grep `backend/live_entry_gate.py` (read-only; no execution, no import).
- **Revision 6** cites no new code and no new broker evidence. It re-reads this document only. The integrity checks
  read the same two files as revision 5 (read-only; no execution, no import).

### Revision history

| Rev | Date | Summary |
|---|---|---|
| 1 | 2026-11-09 | Initial selection of the 25 decisions (DX, DD-1…DD-11, Q1–Q3, M-1…M-10). |
| 2 | 2026-11-09 | Seven review corrections, R2-1 to R2-7 below. The 25-decision structure is unchanged: no decision added, removed or merged. |
| 3 | 2026-11-09 | Three final corrections, R3-1 to R3-3 (records after the R2 table). The 25-decision structure, the R2-2/R2-4/R2-5/R2-6/R2-7 decisions, the v12 design and the 33 broker-assumption classifications are unchanged. No table, column, CHECK or index changes. |
| 4 | 2026-11-09 | One correction, R4 (record after the R3 records). It defines the M-7 per-member worst-case quantity w(m) and therefore S_agg. SELL legs are summed within a version. Alternative versions of one GTT (old and requested modify, create payload, listed) are combined by maximum. Outstanding triggered SELL orders are summed by remaining quantity. An unresolved cancel removes nothing. Every ambiguity fails closed. The R3-2 `w` bullets and its "maximum over that evidence" clause are struck. Unchanged: the 25-decision structure, R3-1, R3-3, the R3-2 membership set and deduplication, the sale gate (𝒢 established, G_max = 0, S_agg = 0), the no-override rule, the EE cancellation scope (step 3), the grace rules, the v12 design, the transition table, the retry rules and the 33 broker-assumption classifications. No table, column, CHECK or index changes. |
| 5 | 2026-11-09 | Two clarifications, R5-1 and R5-2 (records after the R4 record). **R5-1:** an id-less CREATE whose resulting GTTs cannot all be identified makes S_agg UNKNOWN; its payload quantity is never a worst case or a bound; identified resulting GTTs are separate members. **R5-2:** R3-3 condition (e) and G6 require proof for every triggered SELL order and every SELL leg of a triggered GTT; one terminal order never removes a multi-leg GTT. Unchanged: the 25-decision structure, DX and its seven conditions, the R3-2 membership set and deduplication, the R4 formula and MODIFY version handling, the R2-2 cancellation table, the grace start and fresh read, the sale gate, no override and no deferred sale, the v12 design and the 33 broker-assumption classifications. No table, column, CHECK or index changes. |
| 6 | 2026-11-09 | One correction, R6 (record after the R5 records). It defines **T(m)**, the complete triggered-order set of a GTT member (open, partially filled, terminal, cancelled or rejected orders, plus every expected order not yet visible), and keeps **O(m)** as the R4 outstanding subset of T(m) (orders that could still execute). Condition (e) and G6 now evaluate the completeness of T(m); an expected but not-yet-visible order makes the evidence incomplete, and an empty O(m) or visible terminal orders never prove completeness. O(m) is used only for the R4 outstanding exposure. Future S7 test T6. Unchanged: the 25-decision structure, DX and its seven conditions, the R3-2 membership set and deduplication, the R4 formula and quantity rules (including old/requested MODIFY versions), R5-1 and B-24, the R5-2 safeguards, the R3-3 grace start, cancellation rules and fresh read, the G1–G22 numbering and refusal outcomes, the sale gate, no override and no deferred sale, the v12 design and the 33 broker-assumption classifications. No table, column, CHECK or index changes. |

| Id | Review item | Decisions and sections changed | What changed | Why |
|---|---|---|---|---|
| **R2-1** | Mandatory 1: automatic `attribution_cap` retries | DX, DD-4, M-1, M-6, §4.2 rows 1/5/16, §4.4 | A fifth DX effect class, **`caps_sell_exposure`**, with its own send predicate and automation rule: automatic claim and bounded retry, re-evaluated at every send. Classes are now assigned in a fixed precedence order. `reduces_protection` keeps its rev. 1 rule (never automatic while the position is open) and no longer covers the cap. | In rev. 1, DX said a `reduces_protection` op is "never retried automatically while the position is open". §4.4, however, classified `attribution_cap` as `reduces_protection` *and* made it automatic and retried. The DD-4 CHECK allowed the retry, and M-1's reason ("removing a stop belongs to an exit or to the user") denied it. A reason-keyed exception would contradict "automation by effect", so the case gets its own effect class. |
| **R2-2** | Mandatory 2: a cancel whose effect becomes `none` once the position is flat | M-1, M-3, M-7 step 8, §4.2 rows 1/7/16, §4.3 row 2, §4.4 | One table decides the action from the op's **state** at the moment its effect becomes `none`. Terminal ops are never reopened. After `rejected`, only a session-class rejection allows an automatic **new** op. After `not_sent`, a new op is allowed. Every automatic re-claim is bounded per (position, target). | M-1 said the cancel "becomes automatically retryable on the same op". M-3 said "after `rejected`, no automatic retry of any kind", and `rejected` is terminal, so the same op cannot be resent. M-7 step 8 relied on M-1's wording for cancels that may have been rejected. |
| **R2-3** | Mandatory 3: Emergency Exit sell conditions | M-7 steps 4–6 and 8, §5 B-04 posture, §6 A-14 | The sale is allowed only when **no GTT that could still sell the position's shares is unresolved** (G_max = 0). Exclusive attribution is necessary, not sufficient, and no longer authorises a sale on its own. The B-04-dependent relaxation is deferred to Phase 9 evidence. | The rev. 1 step 5 sold under exclusive attribution while a protective GTT's cancel outcome was unknown, so the over-sell outcome rested entirely on unverified broker behaviour (B-04). |
| **R2-4** | Mandatory 4: `trade_tickets.mode` values and mapping | DD-5, DD-6 (preflight), §4.1 comment | The mode domain and the mapping were **verified from source** and documented. One data fact stays UNVERIFIED and is handled fail-closed by a preflight count. | Rev. 1 cited the nullable column, but neither enumerated its values nor showed how and when they are set. |
| **R2-5** | Clarification 5: earliest `absent_after_settle` | DD-8, Q2, §5 B-08 | Earliest time calculated: **`sent_at` + 180 s** plus read durations, with the placeholders. The spacing measure is defined on recorded `broker_reads` timestamps. Counted reads must start after the op became `unknown`. `SETTLE ≥ GRACE` is a static check. | Rev. 1 gave the parameters but not the resulting bound, nor which timestamps the spacing uses (`gtts_at` is a completion time). |
| **R2-6** | Clarification 6: position protection state after a manual resolution | Q3, DD-7, M-5 cond. 3 | A per-outcome table of the resulting `protection_state`. `resolved_manual` never sets protected by itself. A manual "no GTT" outcome requires `user_recreate`, and discharges a flat position's cleanup obligation, with a watch kept. | Rev. 1 did not say what protection state follows a resolution, which left room to read `resolved_manual` as "protected". |
| **R2-7** | Clarification 7: M-6 concurrency | M-6, DD-10 (G4 reset site), §4.2 rows 5/25 | Specifies the shared detector baseline under `_state.cv`, exactly-once detection, gating on the shared flag rather than on the gap, lock ordering, deferral of DB and lock-taking work out of the CAS, a pre-call re-check, and the residual window. | The rev. 1 M-6 asked the CAS to run `reset_cycle_memory()` and to flag `note_resume`. It did not address the deadlock and lock-order hazards that verified code makes concrete, or the case where the heartbeat misses a gap the CAS already consumed. |

**Revision 3 records.** Each record lists the fields the R3 brief requires. The full rules are in the decision rows
tagged **[R3-n]**: DX (R3-1) and M-7 (R3-2, R3-3).

**[R3-1] DX classification precedence**

| Field | Content |
|---|---|
| **Original ambiguity** | The rev. 2 precedence (DX R2-1 row) evaluated `none` (step 2: "every held share keeps valid coverage") **before** `caps_sell_exposure` (step 4). Take an open position of 100 with a protective GTT of 120 and Q_cap = 100. The cap 120 → 100 keeps coverage of all 100 shares, so it matched `none` first. As `none` it would have been sent without re-proving `broken` attribution or Q_cap, without the URGENT finding, and, after a resume, before Stage 1 recomputed attribution (M-6 allows `none` sends with a post-resume read). Rev. 2 also defined `none` by coverage alone, never by the sell-exposure axis. |
| **Corrected rule** | Both axes are computed **independently** at every send, from the send-time read: ΔS (change of the target's sell quantity) and ΔP (change of held shares covered by the target, where a loosen counts as ΔP > 0). **`none`** = increases neither axis (ΔS ≤ 0 and ΔP ≤ 0). New precedence: <br>(1) ΔS > 0 → `increases_sell_exposure`; <br>(2) reason `attribution_cap` with ΔS < 0 → `caps_sell_exposure` if all seven R2-1 conditions hold **at this send**, otherwise `reduces_protection` (fail-closed; steps 3–5 are never consulted); <br>(3) ΔS ≤ 0 and ΔP ≤ 0 → `none`; <br>(4) ΔP > 0 and the M-2 conditions → `redundant`; <br>(5) otherwise → `reduces_protection`. <br>The seven conditions are unchanged. They are checked at the claim, at every `→sending` CAS (first send and each retry), and never carried over from an earlier evaluation. Q_cap may equal the open quantity. The mandatory four-row decision table and the protection exposure of a cap below the open quantity are in DX rev. 3. |
| **Affected decisions and sections** | DX (`none` bullet; R2-1 precedence struck; new rev. 3 row), DD-4 (state transitions; rev. 3 note), M-1 (reason), M-3 (condition 1), M-6 (gate list), §4.1 (DDL comment and notes), §4.2 rows 1/5/16 and "never allowed", §4.4 (`attribution_cap` row and footnote), §6 A-16, §7.1, §7.3, §7.4. |
| **Dependency on earlier decisions** | R2-1 keeps its seven conditions, its automation rule, its Q_cap = 0 rule and its URGENT finding. Only its precedence list is replaced, and its added test "each condition failing alone → `reduces_protection`" is qualified to ops whose request decreases the quantity (ΔS < 0); for ΔS ≥ 0, steps 1 or 3 decide. M-3 condition 1 lacked the R2-1 `caps_sell_exposure` branch that row 16 already had; R3-1 aligns it (no change of rule). DD-4's retry CHECK is unchanged. |
| **Required future tests** | The four mandatory rows (100/120/100 → `caps_sell_exposure`; 100/120/80 → `caps_sell_exposure` only with all seven conditions, position `PROTECTION_NEEDS_REVIEW` after confirmation; 100/100/100 → `none`, no cap op claimed; 100/80/100 → `increases_sell_exposure`, never `caps_sell_exposure`). Each of the seven conditions failing alone with ΔS < 0 → `reduces_protection`, no send. A 100/120/100 cap after a resume is refused until Stage 1 passes (not sent as `none`). A cap whose Q_cap changes between claim and send → no send of the stale quantity. An `exit_fill` 120 → 100 with exclusive attribution stays `none`. Mutations: evaluate `none` before the cap step; carry a claim-time evaluation to the send. |
| **Remaining unknown broker behaviour** | B-04 (over-quantity triggered GTT: why the cap exists, and the Q_cap = 0 residual). B-06 (T1 counted as sellable, which feeds Q_cap). B-14 (positions visibility latency): D§6.3 maps it to `unknown` attribution, so it cannot satisfy condition 2 [V D:660]; the latency value itself is [U]. |

**[R3-2] Complete G_max membership, deduplication and aggregate sell quantity**

| Field | Content |
|---|---|
| **Original ambiguity** | M-7 rev. 2 step 5 called G_max "the most shares any GTT could still sell", which is a quantity, under a name that reads as a count. It enumerated only the linked id, `acknowledged` results, open modify targets, unknown creates and app-linked triggered GTTs. It omitted results of other states, targets of open cancels, ids referenced by unresolved or superseded ops, unlinked GTTs seen by the broker, and ids held only in evidence. It gave no deduplication rule, said nothing on ambiguous ids, and did not say whether two different ids may be treated as one, or whether the sum may be capped at holdings. |
| **Corrected rule** | Per **instrument** (E3 scope; shares are fungible at the broker). A membership set 𝒢 with nine sources (M-7 rev. 3, R3-2 row). Deduplication by broker GTT id (canonical per account and mode); id-less entries keyed by op id and never merged; different ids never merged; an ambiguous id fails closed. **G_max = \|𝒢\|** (count of distinct potentially active sell sources). **S_agg** = the sum of each member's worst-case sell quantity, never capped at holdings. The sale requires 𝒢 established, G_max = 0 **and** S_agg = 0. Otherwise nothing is sold for the instrument, and the result lists every member. **[R4]** Each member's worst-case quantity w(m) is now defined by the M-7 rev. 4 R4 row (record below the R3 records). |
| **Affected decisions and sections** | M-7 (step 5 struck; step 6 wording; rev. 2 tests row qualified; new rev. 3 R3-2 row), §1, §4.2 "never allowed", §5 B-04 and B-24 postures, §6 A-14, §7.1, §7.3, §7.4. |
| **Dependency on earlier decisions** | R2-3 is kept: the sale gate still requires that no GTT that could sell the shares is unresolved, and exclusive attribution is still necessary but not sufficient. R3-2 only makes that set complete and separates count from quantity. M-7 step 3 (which ids EE cancels) is **unchanged**. Members that step 3 does not cancel (unlinked broker GTTs, id-less creates) block the sale and are listed for deletion in Kite. EE never cancels a GTT it cannot prove is the app's (M-2 condition 1 rationale). The E3 snapshot and the D§6.3 attribution table are unchanged. |
| **Required future tests** | One test per membership source (nine), each alone blocking the sale. The same id reached through three sources counts once (G_max = 1). Two different ids with identical shape and quantity count twice, and S_agg sums both. An id-less create is never merged with an unlinked id seen in the read (G_max = 2). Conflicting ids for one op → both members, URGENT finding, refused. A member whose quantity is unknown → S_agg UNKNOWN → refused. No usable read → refused. S_agg is not capped at holdings. Mutations: drop any one source; cap S_agg at holdings; merge by fingerprint; scope the set to the position instead of the instrument. |
| **Remaining unknown broker behaviour** | B-04 (whether Kite stops two SELLs on the same shares: not assumed). B-08 (a GTT absent from the listing cannot be counted: disclosed residual, LIVE-BLOCKER unchanged). B-13 (triggered order link). B-24 (one request may create two GTTs~~, so an id-less create's quantity is a lower bound~~; **[R5-1] superseded by R5: an id-less create whose resulting GTTs are not all identified has w UNKNOWN, so S_agg is UNKNOWN**). B-29 (id reuse: different ids are never merged anyway). B-32 (status vocabulary). Whether the legs of a two-leg GTT are mutually exclusive is [U] and is not relied on (every SELL leg is summed). It is not added to §5: the gate is zero vs non-zero, so the value affects only the reported quantity. **[R5-2] Qualified by R5:** leg exclusivity also bears on whether a triggered member may leave 𝒢 under condition (e). Because it is never relied on, it can only keep a member in 𝒢 (fail-closed), never permit a sale. Still no §5 change. |

**[R3-3] Emergency Exit grace-period semantics**

| Field | Content |
|---|---|
| **Original ambiguity** | M-7 rev. 2 step 4 said EE "waits at most `PROTECTION_INFLIGHT_GRACE_SECONDS`" without saying when the wait starts (EE initiation or the last cancel sent). It did not say what an UNKNOWN cancel means once the wait ends. It did not say what to do when a fresh read still lists a GTT ACTIVE after its cancel was accepted. Rev. 2 also left a "sell anyway" override open as a product question. |
| **Corrected rule** | The grace starts at **T_last**, the latest `sent_at` among the relevant cancels sent during this EE click, and ends at T_last + `PROTECTION_INFLIGHT_GRACE_SECONDS`, or earlier once every relevant cancel has a definite op result. If no cancel was sent in the click, there is no wait. Then one fresh complete read R_EE started at or after max(EE initiation, T_last). A member leaves 𝒢 **only** by the documented confirmation conditions. An UNKNOWN cancel after the grace, an ACTIVE listing in R_EE, an acknowledgement alone, a missing listing, or an unusable read **never** allows the SELL. One authoritative table decides every outcome (M-7 rev. 3, R3-3 row). **No "sell anyway" override, and no background "sell when clear"**: the rev. 2 open product question is closed as "no override" on the R3 brief's instruction. |
| **Affected decisions and sections** | M-7 (step 4 struck; override paragraph struck; new rev. 3 R3-3 row), §4.2 "never allowed", §5 B-28 and B-31 postures, §7.2, §7.3, §7.4. |
| **Dependency on earlier decisions** | The R2-2 cancellation state table is unchanged: R3-3 decides only whether EE may sell, never an op's transitions. `exit_prepare` keeps M-1: one attempt, no automatic retry while the position is open. The cancel index (M-7, DD-1) is unchanged, so a re-run sends no duplicate cancel. DD-8 freshness rules i–iii apply to R_EE unchanged. |
| **Required future tests** | One test per row of the R3-3 table. The grace starts at the last send: a cancel sent 6 s after EE initiation still gets its full grace. UNKNOWN after the grace → no SELL, `refused` with the id. Acknowledged + R_EE ACTIVE → no SELL. Confirmed + R_EE ACTIVE → no SELL, URGENT finding, the terminal op unchanged. Missing → no SELL. No cancel sent in the click → no wait, read immediately. A re-run sends no new cancel and starts no new grace. A static test asserts that no override or deferred-sale path exists. Mutations: start the grace at initiation; treat an acknowledgement as confirmation; treat missing as gone. |
| **Remaining unknown broker behaviour** | B-28 (whether deleted GTTs stay listed with an explicit status: until answered, missing never confirms). B-31 (processing after the client timeout: sets the grace value). B-17 and B-30 (error classes of delete). B-32 (status vocabulary). |

**Revision 4 record.** The full rule is in the M-7 row tagged **[R4]** ("Rev. 4 [R4]: S_agg as the multi-leg
worst-case sell quantity").

**[R4] S_agg multi-leg worst-case sell exposure**

| Field | Content |
|---|---|
| **Original ambiguity** | R3-2 defined S_agg = Σ w(m), but its `w` bullets left four gaps. <br>(1) An id-less create used `requested_quantity`, a single integer (`db.py:2580`), even when `request_payload` carries two SELL legs. <br>(2) A known id took "the old and requested quantities of every unresolved modify" without saying how the legs of each configuration combine, or that the configurations are alternatives of one GTT. <br>(3) A triggered member took "its order's unfilled quantity", as if a GTT could have only one outstanding order. <br>(4) The deduplication bullet made a member's quantity "the maximum over that evidence", which, read literally, takes the maximum rather than the sum of independent legs and orders. A GTT with two SELL legs, a multi-leg modify, or two triggered orders could therefore be under-counted. |
| **Corrected rule** | **S_agg** = the aggregate worst-case quantity of shares that could still be sold, across the entire instrument, by all distinct potentially active GTTs in 𝒢 and their outstanding triggered SELL orders. S_agg = Σ w(m) over 𝒢, deduplicated as in R3-2, with **w(m) = max over the possible versions v of m of L(v), plus Σ r(o) over the outstanding triggered SELL orders o of m**. <br>• L(v) sums **every** potentially executable SELL leg of version v. Legs are never assumed mutually exclusive. <br>• Alternative versions of the same GTT are combined by **maximum**, never summed: listed, create payload, and old and requested modify configurations. <br>• r(o) is the remaining unfilled SELL quantity. Separate orders are **summed**. <br>• An unresolved create counts every SELL leg of its `request_payload`, not `requested_quantity`. <br>• An unresolved modify counts both the old and the requested configuration and takes the greater. <br>• An unresolved cancel removes nothing until an R3-3 condition (a)–(e) holds; an acknowledgement alone never does. <br>• S_agg is never capped at holdings. <br>• Any ambiguity in identity, version, leg quantity, order status or completeness → S_agg UNKNOWN → refuse. <br>The sale gate is unchanged: 𝒢 established, G_max = 0, S_agg = 0, no override. |
| **Affected decisions and sections** | M-7 only (R3-2 row: the `w` bullets and the deduplication clause struck; new rev. 4 R4 row; rev. 2 tests row qualified). Summaries and pointers: header, R3-2 record, §1, §4.1 notes, §4.2 "never allowed", §5 B-24 posture, §7.1 (M-7, M-10), §7.2, §7.4, new §7.5, new §8.4. |
| **Dependency on earlier decisions** | R3-2 is kept: the nine-source membership set 𝒢, deduplication by broker GTT id (id-less entries never merged, different ids never merged, an ambiguous id fails closed), G_max = \|𝒢\|, no cap at holdings, and the sale gate. R4 replaces only R3-2's per-member quantity bullets. R3-3 is kept: the removal conditions (a)–(e) and the grace-outcome table G1–G22 decide when a member, and with it its whole w, leaves 𝒢. R2-3 is kept: exclusive attribution is necessary, never sufficient. M-7 step 3 (which ids EE cancels) and the no-override decision are unchanged. R3-1, R2-2, R2-4…R2-7, the DDL, the transition table and the retry rules are untouched. |
| **Required future tests** (S7; R4 creates or changes no test file) | **T1, two SELL legs:** a listed GTT with two potentially executable SELL legs of 100 and 100, on a position of 100 → w = 200, both quantities contribute to S_agg, not capped at 100. <br>**T2, unresolved multi-leg MODIFY:** old configuration 100 + 100 (L = 200), requested 150 + 30 (L = 180) → w = 200. The reverse case, old 100 + 50 (L = 150) and requested 120 + 120 (L = 240) → w = 240. Each configuration is calculated separately and the greater aggregate is used; never the sum of the two (380 / 390), and never a single leg. An old or requested configuration that cannot be established → S_agg UNKNOWN → refused. <br>**T3, several outstanding triggered SELL orders:** two orders that can each still execute, with remaining 40 and 60 → they contribute 100. A partially filled order contributes its remaining quantity, not its original quantity. <br>**Also:** a two-leg ~~id-less~~ create → the sum of its payload's SELL legs, not `requested_quantity` **[R5-1] (superseded by R5 for an id-less create: w UNKNOWN, S_agg UNKNOWN, see T4 in the [R5-1] record; the payload sum stays the create's version quantity once a resulting GTT id is recorded)**. An incomplete or ambiguous payload → UNKNOWN → refused. An acknowledged cancel leaves w unchanged and the sale refused. An order with an ambiguous id, status or remaining quantity → UNKNOWN → refused. <br>**Mutations that must fail a test:** count one leg; take the maximum of the legs; sum alternative versions; use only the requested version; count one order; use the original order quantity; use `requested_quantity` for a multi-leg create; drop exposure on a cancel acknowledgement. |
| **Remaining unknown broker behaviour** | Whether the legs of a two-leg GTT are mutually exclusive at Kite (OCO behaviour) is [U] and is not relied on: every leg is summed. B-04 (whether Kite stops two SELLs on the same shares: not assumed). B-13 (triggered order link and visibility: a triggered leg with no visible order counts at its full quantity). B-24 (one request may create two GTTs~~: an id-less create's w is a lower bound~~; **[R5-1] superseded by R5: an id-less create whose resulting GTTs are not all identified has w UNKNOWN**). B-32 (status vocabulary: an unknown order or GTT status → UNKNOWN). No §5 classification changes: the gate is zero against non-zero, so these unknowns affect only the reported quantity, not when EE sells. **[R5-2] Qualified by R5:** OCO exclusivity also bears on whether a triggered multi-leg member may leave 𝒢 under condition (e). Because it is never relied on, it can only keep the member in 𝒢 and the sale refused (fail-closed). Still no §5 classification change. |

**Revision 5 records.** The full rules are in the M-7 rows tagged **[R5-1]** ("Rev. 5 [R5-1]: id-less CREATE
multiplicity") and **[R5-2]** ("Rev. 5 [R5-2]: condition (e) for triggered multi-leg GTTs").

**[R5-1] Id-less CREATE and B-24 multiplicity**

| Field | Content |
|---|---|
| **Original ambiguity** | R4 case 2 kept an id-less create's payload quantity "as a **lower bound**, because one request may have produced more than one GTT (B-24)". The B-24 posture said the same. S_agg is defined as the worst-case aggregate. A lower bound is not a worst case. Read literally, S_agg could report the payload sum (for example 200) as if it were the instrument's worst-case exposure, while the request may have created two or more GTTs. |
| **Corrected rule** | Three facts are kept apart: <br>(i) a **known request payload quantity**: what **one** GTT made from the request could sell; <br>(ii) a **proven complete set of resulting GTTs**: every GTT the request produced is identified by broker GTT id; <br>(iii) an **unknown number or identity** of resulting GTTs. <br>(i) never establishes (ii) and never rules out (iii). <br>An id-less create (source 7, key `op:<op_id>`) whose resulting GTTs' number or identity is not established has **w = UNKNOWN, so S_agg = UNKNOWN**. Under B-24 this covers every create with no recorded result id. The payload quantity is never presented as the worst case or as a bound. <br>Each independently identified resulting GTT is a **separate member**, deduplicated by broker GTT id (R3-2), with its own w (R4). Recording one id never proves that only one GTT was created. A sibling is counted through source 6 when a complete read lists it, or as an ambiguous-id candidate. <br>Id-less entries are never merged. Any ambiguity in multiplicity, identity, payload completeness or resulting GTT state fails closed. An unresolved id-less create never permits a sale (G21). |
| **Affected decisions and sections** | M-7 (R4 row case 2: the lower-bound sentence struck; R3-2 row: id-less deduplication bullet and the UNKNOWN bullet get pointers; new Rev. 5 R5-1 row; G21 cell). Records and summaries: header, revision history, R3-2 and R4 records, §1, §4.1 notes, §4.2 "never allowed", §5 intro and B-24 posture, §7.1 (M-7, M-10), §7.2, new §7.6, new §8.5. |
| **Dependency on earlier decisions** | R3-2 is kept: the nine membership sources, deduplication by broker GTT id, id-less entries keyed by op id and never merged, different ids never merged, ambiguous ids failing closed, G_max = \|𝒢\|, no cap at holdings. Its rule that an id-less entry becomes an id's source once the op records a `result_gtt_id` is kept. R4 is kept: the w(m) formula; for a create **with** an id, the payload version (every SELL leg, never `requested_quantity` alone) is still a version in V(m), and the payload fail-closed list is unchanged. R3-3 (d) is kept as the only way an id-less entry leaves 𝒢. B-24 keeps **LIVE-P9 (disclosed residual)**. |
| **Required future tests** (S7; R5 creates or changes no test file) | **T4, id-less CREATE multiplicity:** one id-less create whose `request_payload` has a known SELL quantity (two legs, 100 + 100). B-24 means it may have created more than one GTT. No result id is recorded, and the resulting GTT count and identities cannot be established. Expected: w = UNKNOWN, **S_agg = UNKNOWN** (not 200 and not "at least 200"); G21; Emergency Exit refuses; the result lists `op:<op_id>` with w UNKNOWN. <br>**T4 positive control:** the request's resulting GTTs X and Y are both independently identified (X by the op's recorded `result_gtt_id`, Y by its own id in a complete read). Expected: two members, w(X) and w(Y) each computed under R4 and summed into S_agg. X reached through several sources counts once. X and Y are never merged because their payloads or fingerprints match. <br>**Mutations that must fail a test:** report the payload sum as the id-less member's w; report it as a bound; merge two id-less entries; drop a listed sibling once one id is recorded; merge X and Y by fingerprint. |
| **Remaining unknown broker behaviour** | B-24 (edge retries to the OMS: how many GTTs one request can create is unknown, so it is never assumed to be one). B-08 (a resulting GTT that no complete read lists cannot be counted: the existing disclosed residual, LIVE-BLOCKER unchanged). No §5 classification changes. |

**[R5-2] R3-3 condition (e) for triggered multi-leg GTTs**

| Field | Content |
|---|---|
| **Original ambiguity** | R3-3 condition (e) removed a triggered member when "R_EE shows **its order** terminal with zero pending quantity, and the resulting fill is recorded". Under the R4 multi-leg model, a GTT may have several SELL legs and several triggered SELL orders. One terminal order says nothing about the others. The G6 cell ("w = unfilled order quantity") could be read the same way: once that one order is terminal, the member looked harmless. |
| **Corrected rule** | **[R6] Items (1)–(5) are restated by R6 over T(m), the complete triggered-order set, with O(m) kept as its outstanding subset (see the [R6] record). "Outstanding … terminal" in item (1) is superseded by R6: an outstanding order is by definition not terminal.** A triggered member leaves 𝒢 under (e) only when complete, unambiguous evidence establishes **all** of the following: <br>(1) every ~~outstanding~~ **[R6] (superseded by R6: every order in T(m), with T(m) established as complete)** triggered SELL order of the GTT is terminal; <br>(2) every such order has zero remaining unfilled quantity; <br>(3) every other SELL leg of the GTT is proven unable to execute; <br>(4) all resulting fills are recorded and reconciled against the relevant position; <br>(5) the evidence covers the complete GTT, with all its triggered orders and legs. <br>Otherwise: <br>• the member stays in 𝒢; <br>• its known remaining exposure stays in S_agg (R4); <br>• if that exposure or the completeness cannot be established, S_agg = UNKNOWN; <br>• Emergency Exit refuses. <br>A single terminal order is never proof that a multi-leg GTT is harmless. G6 is reworded so that it removes a member only under the full rev. 5 (e). |
| **Affected decisions and sections** | M-7 (R3-3 row: condition (e) struck and replaced, "never sufficient" list extended; G6 row; R3-2 row source 8 pointer; R4 row "potentially executable SELL leg" and case 5 pointers; new Rev. 5 R5-2 row). Records and summaries: header, revision history, R3-2 and R4 records, §1, §4.2 "never allowed", §7.1 (M-7), §7.2, §7.4 (R3-3 row pointer), new §7.6, new §8.5. |
| **Dependency on earlier decisions** | R3-3 is kept: the grace starts at T_last, then one fresh complete R_EE; conditions (a)–(d) are unchanged; C and D are unchanged; there is no override and no deferred sale. The G1–G22 numbering and row order are unchanged. Only G6's cells change, and G6 still refuses wherever complete evidence is unavailable. R4 is kept: L(v), r(o), the B-13 full-leg rule and "if which leg triggered cannot be established, every SELL leg counts". The R2-2 cancellation table and the cancellation acknowledgement rules are unchanged. |
| **Required future tests** (S7; R5 creates or changes no test file) | **T5, partially resolved multi-leg triggered GTT:** a two-leg GTT with two potentially executable SELL legs of 100 and 100, listed `triggered` in R_EE. The triggered order of the first leg is terminal (COMPLETE, filled 100, nothing left to fill) and its fill is recorded. The second leg, or its triggered order, can still execute (no order seen, or an order open with 100 remaining). Expected: the member stays in 𝒢 (G6: not removed); its remaining exposure of 100 is in S_agg; Emergency Exit refuses. <br>**T5 fail-closed variant:** the status or quantity of the second leg, or of its order, cannot be established (unknown status, ambiguous order id, leg missing from the read). Expected: S_agg = UNKNOWN; Emergency Exit refuses. <br>**Also (keeps (e) reachable):** both legs' orders are terminal with nothing left to fill, every fill is recorded and reconciled, and the evidence is complete. Expected: the member is removed under (e), and the outcome is as G1. <br>**Mutations that must fail a test:** remove the member on one terminal order; treat the `triggered` status as proof that the other leg is dead; assume OCO exclusivity; skip fill reconciliation; ignore an expected order that is not yet visible (B-13). |
| **Remaining unknown broker behaviour** | Two-leg OCO exclusivity at Kite [U]: not relied on, so under R5-2 a GTT triggered on one leg stays in 𝒢 until the other leg is proven unable to execute. B-13 (triggered order link and visibility: an unseen expected order means incomplete evidence). B-32 (order and GTT status vocabulary: an unknown status never proves terminal). No §5 classification changes: these unknowns can only keep a member in 𝒢. |

**Revision 6 record.** The full rule is in the M-7 row tagged **[R6]** ("Rev. 6 [R6]: complete triggered-order set
T(m) and outstanding subset O(m)") and in condition (e) rev. 6 of the M-7 R3-3 row.

**[R6] Complete triggered-order set T(m) and outstanding-order subset O(m)**

| Field | Content |
|---|---|
| **Original ambiguity** | R4 defined **O(m)** as "the set of outstanding triggered SELL orders of m": the orders that could still execute (R4 case 5). R5-2 condition (e) item 1 then required "every **outstanding** triggered SELL order associated with the GTT" to be **terminal**. Read with the R4 meaning, that is self-contradictory, because an outstanding order is by definition not terminal. Read loosely, it evaluates only the orders currently in O(m), so an empty O(m) (every visible order terminal, an expected order not yet listed) could look like proof that the GTT is harmless. Rev. 5 named no set for "all triggered orders of m, including finished and not-yet-visible ones". |
| **Corrected rule** | Two sets, used for different purposes: <br>• **T(m), the complete triggered-order set** of member m: every triggered SELL order associated with m, whether currently open, partially filled, terminal, cancelled or rejected, **plus** every order whose existence the evidence on the GTT's trigger and legs indicates (for example a leg evidenced as triggered) but which is not yet visible in the broker listing (B-13). T(m) is a historical and current completeness concept, not a snapshot of open orders. An expected but not-yet-visible order is never omitted: it makes the evidence incomplete. If the evidence cannot establish which orders should exist, T(m) is not established as complete. <br>• **O(m), the outstanding triggered-order subset** (R4, unchanged): O(m) ⊆ T(m), the orders that could still execute: open, partially filled, or otherwise unresolved (unknown status, ambiguous identity, or expected but not yet visible). A terminal order proven incapable of further execution is **not** in O(m) and contributes 0. <br>**Condition (e) rev. 6** evaluates T(m). A triggered member leaves 𝒢 only when complete, unambiguous evidence establishes all of: (1) T(m) is complete; (2) every order in T(m) is proven terminal; (3) every order in T(m) has zero remaining unfilled quantity, any unfilled quantity explicitly reported by the broker as cancelled or rejected, never inferred from missing data; (4) every SELL leg of the GTT is proven unable to execute; (5) all resulting fills are recorded and reconciled against the relevant position; (6) the evidence covers the entire GTT, all its SELL legs and every order in T(m). Otherwise the member stays in 𝒢, all its known outstanding exposure stays in S_agg under the unchanged R4 calculation, S_agg = UNKNOWN if that exposure or the completeness of T(m) cannot be established, and Emergency Exit refuses. <br>**S_agg is unchanged.** w(m) = max { L(v) : v ∈ V(m) } + Σ { r(o) : o ∈ O(m) }. O(m), not T(m), feeds the exposure term, so a proven terminal order is never counted as outstanding sell exposure. <br>A terminal order is never an outstanding order, and an empty O(m) is never proof that T(m) is complete or harmless. |
| **Affected decisions and sections** | M-7 only (R4 row: `O(m)` term and case 5 pointers; R3-3 row: rev. 5 condition (e) struck and restated as rev. 6, "never sufficient" list extended; R3-2 row: source 8 pointer; Rev. 5 R5-2 row: pointers; new Rev. 6 R6 row; G6 cells; G20 pointer). Records and summaries: header, evidence labels, starting state, sources, revision history, [R5-2] record pointer, §1, §4.1 notes, §4.2 "never allowed", §5 intro, §7.1 (M-7, M-10), §7.2, §7.4 (R3-3 row, item 4), §7.6 (R5-2 row, item 3, item 6), new §7.7, new §8.6, closing line. |
| **Dependency on earlier decisions** | R4 is kept: the O(m) meaning, the w(m) and S_agg formula, L(v), r(o), the maximum across alternative versions (listed, create payload, old and requested MODIFY), the B-13 rule (a leg that triggered without a visible order counts at its full leg quantity, once), and every fail-closed rule. R5-2 is kept: one terminal order never removes a multi-leg GTT; `triggered`, OCO and the absence of an order are never proof; fills must be reconciled; an unseen expected order means incomplete evidence. R6 only names the set that R5-2's completeness requirement already referred to. R5-1 and B-24 are untouched. R3-3 is kept: the grace start at T_last, one fresh complete R_EE, conditions (a)–(d), C and D, the cancellation rules (G4, G5, the R2-2 table), no override and no deferred sale. The G1–G22 numbering and every refusal outcome are unchanged; only G6's cells are reworded. R3-2 membership and deduplication and DX are untouched. |
| **Required future tests** (S7; R6 creates or changes no test file) | **T6, expected triggered order not yet visible:** a multi-leg GTT has triggered. All currently visible triggered SELL orders are terminal with zero remaining quantity (O(m) restricted to visible orders would be empty). The complete trigger and leg evidence indicates that another triggered SELL order is expected, and that order is not yet visible in the broker listing. Expected: T(m) cannot be established as complete; the member stays in 𝒢 (G6: not removed); S_agg = UNKNOWN if the missing order's exposure cannot be established (otherwise its leg quantity is in S_agg under the R4 B-13 rule); Emergency Exit refuses. <br>**Mutations that must fail a test:** evaluate (e) over O(m) instead of T(m); treat an empty O(m) as proof that T(m) is complete; drop an expected but not-yet-visible order from T(m); count a proven terminal order in Σ r(o); infer an unfilled quantity as cancelled from missing data. |
| **Remaining unknown broker behaviour** | B-13 (triggered order link and visibility; whether and when each expected order becomes visible): an expected order that is not yet visible keeps T(m) incomplete. Which evidence shows that a leg has triggered, and how many orders a triggered leg produces, are not established beyond what the existing evidence states; where they cannot be established, T(m) is not complete (fail-closed). Two-leg OCO exclusivity [U] and B-32 (status vocabulary) as in R5-2. No §5 classification or posture change: these unknowns can only keep a member in 𝒢. |

---

## 1. Summary

**What this resolves.** It resolves:

- DD-1 to DD-11 (RR§12);
- the D§11.2 Q1–Q3 sub-choices left open in RR§6.2;
- the ten mandatory decisions M-1 to M-10.

That is 24 decisions, plus the organising decision DX (§2): 25 in total. Each one records eight fields:

- Problem;
- Options considered;
- Selected approach;
- Reason;
- State-transition implications;
- Database implications;
- Required tests;
- Whether it blocks offline engineering or live activation.

**One organising principle (§2, decision DX).** Every protection operation is classified, at every send, on two
independent axes:

- **sell exposure:** can this request cause shares to be sold that the app does not hold or does not own?
- **protection exposure:** does it leave shares the app holds without a valid stop?

The Phase 2 design treated "cancel" and "modify-down" as exposure-reducing by kind (D§4.2). M-1 and M-2 show
why that is not enough: cancelling the only valid stop of an open position reduces sell exposure but removes
protection. Automation is therefore decided by **effect**, not by kind.

**Amendments.** Several decisions amend the Phase 2 design or the readiness review. They are listed in §6:
cancel retries, the cancel index, `absent_after_settle`, resolution outcomes, `direction` values, event types, and
the reason list.

**Revision 2 (§7.3).** Seven review corrections (R2-1 to R2-7) remove four contradictions and fill three gaps:

- `attribution_cap` retries (a fifth DX effect class);
- cancels that become `none` after the position is flat (a state table; terminal ops are never resent);
- the Emergency Exit sale gate (G_max = 0; exclusive attribution is not sufficient; membership made complete by
  **[R3-2]**);
- `mode` (verified from source);
- the earliest settle time (`sent_at` + 180 s plus read latencies);
- protection state after a manual resolution (never confirmed by the outcome alone);
- M-6 concurrency.

The structure of 25 decisions is unchanged.

**Revision 3 (§7.4).** Three final corrections:

- **[R3-1]** DX evaluates the two exposure axes independently. `none` means "increases neither axis". An
  `attribution_cap` quantity decrease is classified `caps_sell_exposure` (all seven conditions, re-proved at every
  send) or `reduces_protection`, **before** `none` is considered. Q_cap may equal the open quantity.
- **[R3-2]** The Emergency Exit sale gate uses a complete, instrument-wide set of potentially active sell sources
  (nine sources), deduplicated by broker GTT id. **G_max** is the count of that set and **S_agg** its aggregate
  worst-case sell quantity (the per-member quantity is refined by **[R4]**). The sale requires both to be established
  and zero.
- **[R3-3]** The Emergency Exit grace starts at the last relevant cancel sent in the click. An UNKNOWN cancel after the
  grace, an ACTIVE listing, or an acknowledgement alone never allows the sale. There is no override.

The structure of 25 decisions is still unchanged.

**Revision 4 (§7.5).** One correction, **[R4]**. S_agg is the aggregate worst-case quantity of shares that could still be
sold, across the instrument, by every distinct potentially active GTT and its outstanding triggered SELL orders:

- every potentially executable SELL leg is summed within a GTT version;
- alternative versions of one GTT (old and requested modify, create payload, listed) are combined by maximum;
- outstanding triggered SELL orders are summed by remaining quantity;
- an unresolved create counts its whole payload, and an unresolved cancel removes nothing;
- any ambiguity fails closed.

The sale gate, the no-override rule and the structure of 25 decisions are unchanged.

**Revision 5 (§7.6).** Two clarifications:

- **[R5-1]** An id-less CREATE whose resulting GTTs cannot all be identified (B-24) has w UNKNOWN, so S_agg is
  UNKNOWN. Its payload quantity is never presented as a worst case or a bound. Identified resulting GTTs are separate
  members, and id-less entries are never merged.
- **[R5-2]** A triggered GTT leaves 𝒢 under condition (e) only when every triggered SELL order is terminal with
  nothing left to fill, every other SELL leg is proven unable to execute, every fill is reconciled, and the evidence
  covers the whole GTT. One terminal order never removes a multi-leg GTT (G6).

The sale gate, the no-override rule and the structure of 25 decisions are unchanged.

**Revision 6 (§7.7).** One correction, **[R6]**, to terminology inside M-7:

- **T(m)** is the complete triggered-order set of a GTT member: open, partially filled, terminal, cancelled and
  rejected orders, plus every expected order not yet visible. **O(m)** stays the R4 outstanding subset of T(m): the
  orders that could still execute.
- Condition (e) and G6 evaluate the completeness of T(m). An expected but not-yet-visible order makes the evidence
  incomplete. An empty O(m), or visible terminal orders, never prove completeness.
- O(m) feeds only the unchanged R4 exposure term, so a proven terminal order never counts as outstanding exposure.

The sale gate, the R4 formula, the no-override rule and the structure of 25 decisions are unchanged.

**Outcome (§7).**

- All 25 decisions are selected and ready for approval.
- The open items are broker facts: they fix parameter values, or whether a fail-closed path can later be
  relaxed. None of them prevents building and testing the engine offline.
- **Live activation stays NO-GO.**

---

## 2. Decision DX: dual-exposure classification

| Field | Content |
|---|---|
| **Problem** | D§4.2 (D:441-446) puts cancel and modify-down in the "cannot increase sell exposure, automatically retried" class. That is true for sell exposure only. A cancel of the sole protective GTT of an open position, or a modify that loosens the trigger, increases the time held shares spend unprotected. An engine that auto-retries by kind could strip protection without a human (M-1, M-2). |
| **Options** | (a) Keep classification by kind (D§4.2). (b) Classify by reason, fixed at claim time. (c) **Classify by effect**, evaluated against the position and a fresh read **at every `→sending` compare-and-set**, with the reason as an upper bound. |
| **Selected** [S] | **(c).** Effect classes, evaluated inside the fenced CAS that commits `sending`: <br>• **`none`**: no shares need this stop. The position is broker-proven flat, or the change keeps coverage of every held share (a modify down to exactly the open quantity; a trailing tighten). **[R3-1] Restated on both axes:** `none` is an op that increases neither sell exposure nor protection exposure. It is assigned only after the `attribution_cap` step of the rev. 3 precedence, so an `attribution_cap` quantity decrease is never `none` (see the rev. 3 row). <br>• **`redundant`**: a different app-linked GTT is verified to protect the full open quantity (M-2 conditions). <br>• **`reduces_protection`**: held shares lose some or all valid stop coverage: a sole-GTT cancel, a loosen, or a modify down below the open quantity. <br>• **`increases_sell_exposure`**: a create, or a modify up. <br>**Automation rule:** <br>• `none` / `redundant`: automatic claim and bounded retry (§5.3); <br>• `reduces_protection`: only within an exit claim that owns the position, or by an explicit user action; one attempt; never retried automatically while the position is open; <br>• `increases_sell_exposure`: first claim only through the DD-9 predicate; never retried (`attempts ≤ 1`). |
| **Reason** | The effect depends on facts that change between claim and send: fills, exits, another GTT confirmed or lost, a resume. Evaluating at the send makes the decision under the same write lock as the commit that precedes the broker call, so no stale classification can authorise a send [I]. |
| **State transitions** | Adds a predicate to D§4.3 rows 5 and 16 (`intent→sending`, `unknown→sending`). If the effect class no longer permits the send, the op does not send: <br>• from `intent`: → `not_sent` (nothing left the process); <br>• from `unknown`: the op stays `unknown`, and resolution by reads continues. |
| **Database** | `reason` and `direction` CHECKs bound what an op may ever do (DD-3, DD-4). The effect class itself is **not stored as authority**. It is recorded in `evidence` (JSON) for each send, with the `broker_reads.id` it relied on. Not stored as a column, because a column would invite reuse of a stale value. |
| **Required tests** | (1) A cancel of the sole linked GTT on an open position is never auto-sent and never auto-retried; the same op auto-retries once the position is broker-flat **while it is `unknown`; a terminal op is never resent (M-1 rev. 2) [R2-2]**. (2) A loosen is never automatic. (3) The effect class is re-evaluated after a fill between claim and send. (4) A mutation that classifies by kind must be caught. |
| **Blocks** | Offline engineering: **yes, until approved**. It shapes S1 (CHECKs) and S4 (the CAS). Live: no additional blocker. |
| **Rev. 2 amendment [R2-1]: a fifth effect class and a fixed precedence** | **Why.** Rev. 1 classed `attribution_cap` (a modify down to the broker-proven sellable quantity under `broken` attribution, D§6.3) as `reduces_protection`. Under the rule above, that class is never automatic while the position is open, yet §4.4 made it automatic and retried. The cap reduces protection *and* reduces sell exposure, on the ownership side of axis 1 ("shares the app does not own"). None of the four rev. 1 classes describes an op that moves both axes, one down and one up. <br>**Added class `caps_sell_exposure`** [S]. A modify qualifies only if **all** of the following hold in one evaluation, inside the `→sending` CAS: <br>1. the only change is a quantity decrease; trigger and limit are unchanged (DD-4 direction `down`, reason `attribution_cap`); <br>2. a complete read satisfying DD-8 freshness (including rule iii) puts the instrument's attribution in `broken` (D§6.3), evaluated over that read; <br>3. the requested quantity equals Q_cap = min(app open quantity, broker_total − baseline), computed from **that** read (D:682); <br>4. 0 < Q_cap < the target's current quantity in that read; <br>5. the target is the position's linked `kite_gtt_id`, `active` in that read, in the expected pre-state; <br>6. no exit is in flight (an exit owner cancels instead, M-7); <br>7. no unresolved resume (M-6 gate). <br>~~**Precedence** (the first matching class wins):~~ <br>~~(1) a create or any quantity increase → `increases_sell_exposure`;~~ <br>~~(2) every held share keeps valid coverage → `none`;~~ <br>~~(3) the M-2 conditions → `redundant`;~~ <br>~~(4) the conditions above → `caps_sell_exposure`;~~ <br>~~(5) otherwise → `reduces_protection`.~~ <br>**[R3-1] The rev. 2 precedence above is superseded by the rev. 3 precedence (row below). Step (2) could classify a cap as `none` before step (4) was evaluated.** <br>A loosen therefore always falls to `reduces_protection`, because condition 1 excludes it (unchanged under rev. 3). <br>**Automation rule for `caps_sell_exposure`:** automatic claim, with an URGENT finding (it is reported, never silent); bounded automatic retry under M-3 (a read-back shows the pre-state, backoff, cap). At every send, conditions 1–7 are re-evaluated. If attribution is no longer `broken`, or Q_cap has changed: `intent → not_sent`, or `unknown` stays `unknown`. A new op may then be claimed with the new Q_cap. <br>**Q_cap = 0** (the broker shows no app-sellable share while the GTT is armed): no modify can express it, and a cancel would be `reduces_protection`. So: **no automatic action**, the position goes to `PROTECTION_NEEDS_REVIEW`, and an URGENT finding is raised. This is a disclosed residual that rests on B-04. <br>**Unchanged:** the four rev. 1 classes and their rules. `reduces_protection` is still never automatic while the position is open. The effect class is still not stored as a column. <br>**Added tests:** each of conditions 1–7 failing alone makes the op `reduces_protection`, so it is not automatic **[R3-1] (for a request that decreases the target's quantity in the send-time read, ΔS < 0; when ΔS ≥ 0, rev. 3 steps 1 or 3 decide, see examples 3–4 of the rev. 3 table)**. Attribution clears between claim and send → no send. Q_cap changes between attempts → no resend of the stale quantity. Q_cap = 0 → needs review, no cancel. A combined cap + loosen → `reduces_protection`. Mutation: classify the cap as `none`. |
| **Rev. 3 amendment [R3-1]: independent axes and the corrected precedence** | **Why.** Under the rev. 2 precedence, a cap from 120 to Q_cap = 100 on an open position of 100 keeps coverage of every held share, so it matched `none` (step 2) before `caps_sell_exposure` (step 4) was evaluated. As `none` it would be sent without re-proving `broken` attribution or Q_cap, without the URGENT finding, and after a resume before Stage 1 recomputes attribution (M-6 lets `none` sends through with a post-resume read). <br><br>**The two axes, computed independently at every evaluation** [S], from the complete read R used for that evaluation (DD-8 rules i–iii): <br>• q0 = the target's current SELL leg quantity in R (0 for a create); q1 = the requested quantity (0 for a cancel); N = the app open quantity. <br>• **Sell exposure: ΔS = q1 − q0.** <br>• **Protection exposure: ΔP = C0 − C1**, where C = min(q, N) is the number of held shares the target covers while its trigger and limit are valid. Any trigger or limit loosen counts as ΔP > 0. <br>• ΔS, ΔP and R's `broker_reads.id` are recorded in `evidence` for each evaluation. They are never stored as a column (unchanged). <br><br>**Definitions** [S]: <br>• **`none`**: increases neither axis (ΔS ≤ 0 and ΔP ≤ 0). Examples: a cancel on a broker-flat position; an `exit_fill` modify down to exactly N; a trailing tighten; a request equal to the target's current values. <br>• **`caps_sell_exposure`**: an `attribution_cap` quantity decrease (ΔS < 0) for which **all seven** R2-1 conditions hold. Q_cap may **equal** N: an excessive GTT quantity is reduced to Q_cap whether or not that leaves every held share covered. <br><br>**Precedence (the first matching step wins)** [S]: <br>(1) ΔS > 0 (a create, or any quantity increase) → `increases_sell_exposure`; <br>(2) reason `attribution_cap` and ΔS < 0 → `caps_sell_exposure` if conditions 1–7 all hold in this evaluation; otherwise `reduces_protection` (fail-closed: not automatic). Steps 3–5 are never consulted for such an op, because its only justification is the attribution evidence (DX: "the reason as an upper bound"); <br>(3) ΔS ≤ 0 and ΔP ≤ 0 → `none`; <br>(4) ΔP > 0 and the M-2 conditions (`duplicate_cleanup`) → `redundant`; <br>(5) otherwise → `reduces_protection`. <br><br>**When the seven conditions are checked.** At the claim (§4.2 row 1), and again at **every** `→sending` CAS: the first send (row 5) and every retry (row 16). Each check uses a complete read satisfying DD-8; a retry's read must also start after the previous attempt's `sent_at` + grace (M-3 condition 2). A result computed at the claim or at an earlier attempt never authorises a later send. <br><br>**Mandatory decision table:** see "DX rev. 3 decision table [R3-1]" directly below this table.<br><br>**Q_cap below the open quantity (row 2).** The same complete, fresh read that puts the instrument in `broken` shows only 80 shares above the user's baseline. A GTT for 100 could sell 20 shares the broker attributes to the user, or go short (B-04). The cap trades 20 shares of app-recorded protection for the removal of that sell exposure. **It is not refused** when all seven conditions hold, because it is the R2-1/D§6.3 resize-down. But it is never described as full protection: <br>• after confirmation, the position is **`PROTECTION_NEEDS_REVIEW`** (as for Q_cap = 0), and the URGENT finding states the uncovered quantity N − Q_cap; <br>• no automatic resize-up follows (DD-9 condition 5 refuses `broken`); <br>• a visibility lag cannot produce this case: D§6.3 maps `FILL_NOT_YET_VISIBLE` to `unknown`, which fails condition 2, and `SHORTFALL` needs two consecutive complete reads [V D:655, D:660]. <br>If any condition fails, the op is `reduces_protection` and is refused for automation: `intent → not_sent`, or `unknown` stays `unknown` (rev. 2 rule, unchanged). <br><br>**Unchanged:** the seven conditions, the Q_cap = 0 rule, the URGENT finding, the bounded retry under M-3, and the other four classes' automation rules. <br>**Added tests:** the four rows above. Each condition failing alone with ΔS < 0 → `reduces_protection`. The 100/120/100 cap after a resume is refused until Stage 1 passes (not sent as `none`). An `exit_fill` 120 → 100 with `exclusive` attribution stays `none`. ΔS and ΔP are recorded for each evaluation. Mutations: evaluate `none` before step 2; reuse a claim-time evaluation at the send; compute ΔP from the other axis. |

**DX rev. 3 decision table [R3-1]** (mandatory examples; attribution `broken`; ΔS and ΔP as defined in the rev. 3 row):

| Open qty | Existing GTT qty | Q_cap | ΔS / ΔP | Classification | Protection exposure and action |
|---:|---:|---:|---|---|---|
| 100 | 120 | 100 | −20 / 0 | `caps_sell_exposure` | All 100 held shares stay covered; 20 shares of excess sell exposure are removed. Automatic, URGENT finding, bounded retry while the conditions hold. |
| 100 | 120 | 80 | −40 / +20 | `caps_sell_exposure`, **only if all seven conditions hold** at that send; otherwise `reduces_protection` (no send) | **Full protection is NOT preserved:** up to N − Q_cap = 20 app-recorded shares lose stop coverage. Not refused; position `PROTECTION_NEEDS_REVIEW` after confirmation (see "Q_cap below the open quantity" in the rev. 3 row above). |
| 100 | 100 | 100 | 0 / 0 | `none` | Nothing to cap; no `attribution_cap` op is claimed (the automatic claim requires `caps_sell_exposure`). An already-open cap op whose target now shows the requested quantity is confirmed by its read-back (row 15). |
| 100 | 80 | 100 | +20 / −20 | **Not** `caps_sell_exposure`: `increases_sell_exposure` (step 1) | An `attribution_cap` op cannot express it (direction `down` by CHECK). A resize-up is a `fill_resize`, refused under `broken` (DD-9 condition 5; D§6.3). 20 shares stay uncovered; the position is not `PROTECTION_CONFIRMED`; manual review. |

---

## 3. Decisions

### DD-1: adopt the amended dark DDL (D§3/§11.2 Q1–Q3)

| Field | Content |
|---|---|
| **Problem** | The dark DDL still has the Q1–Q3 defects (RR§6.3): `superseded` is terminal and leaves the open index; `abandoned` conflates two risks; a single open index blocks an Emergency Exit cancel. Verified at `db.py:2538-2603` [V]. |
| **Options** | (a) Build Phase 3 on the current DDL and fix it at registration. (b) Apply D§11.2 verbatim. (c) **Apply D§11.2 with the corrections that DX, M-1…M-9 and Q1–Q3 require.** |
| **Selected** [S] | **(c)**, as stage S1, on the unregistered schema. The consolidated DDL is in §4.1. Beyond D§11.2: <br>• the sent-state CHECK gains `absent_after_settle`; <br>• `NOT (state='intent' AND superseded_at IS NOT NULL)`; <br>• `(state ∈ open) = (resolved_at IS NULL)`; <br>• `(state='resolved_manual') = (resolution_id IS NOT NULL)`; <br>• `reason` / `direction` CHECKs (DD-3, DD-4); <br>• `updated_at` (DD-7); <br>• the cancel index keyed on `(position_id, target_gtt_id)` (M-7); <br>• the resolution table with `op_id UNIQUE` and `evidence_read_id NOT NULL` (Q3). |
| **Reason** | The table is unregistered and empty everywhere (D:1137-1139 [V]), so the amendment costs one DDL edit now. After registration it would cost a table rebuild on the account database [I]. |
| **State transitions** | The amended model in §4.2 (11 states: 5 open, 6 terminal; overlay not a state). |
| **Database** | Dark DDL only (S1). Not registered (DD-2). `dark_protection_schema_status`, `_m012` and `_r012` cover the new table and indexes. Rollback drops them and refuses while any of them holds data. |
| **Required tests** | Replace the old-enum cases at `tests/test_dark_protection_schema.py:325, 373, 456-458, 530` (RR§6.2). <br>For every CHECK: a positive and a negative row. <br>Index partition: <br>• a cancel coexists with an unresolved create or modify; <br>• two open changes are refused; <br>• two open cancels on the **same** target are refused; on **different** targets they are allowed. <br>Mutations: <br>• drop `needs_review` from the change index; <br>• merge the indexes; <br>• allow `not_sent` with `sent_at`; <br>• allow `absent_after_settle` for a modify; <br>• allow the overlay on `intent`. <br>Apply/rollback byte-identical; refusal while holding data (now including resolution rows). |
| **Blocks** | Offline engineering: **yes, it is S1**, the first engineering step after G0. Live: no. |

### DD-2: v12 stays dark through Phase 3

| Field | Content |
|---|---|
| **Problem** | Registering v12 migrates the real account database on the next start (`migrate()` applies every pending entry, `db.py:2358-2410` [V via RR§7.1]). Phase 3 is offline only. |
| **Options** | (a) Register v12 in Phase 3. (b) **Keep it dark**: renumber, test on scratch databases, register later under a separate gate. (c) Register it with a feature flag. |
| **Selected** [S] | **(b).** `_m012_*`/`_r012_*` exist and are tested (M-9). `MIGRATIONS` stays v1…v11, so `LATEST_SCHEMA_VERSION = 11`. Registration is a separate authorisation, taken with the first engine wiring (Phase 4/5), after: <br>• the operator backup (Ph2§11); <br>• the DD-6 preflight. |
| **Reason** | No Phase 3 test needs the journal on the real database (DD-11). A flag (c) would still migrate the database, because the migration runs regardless of any flag [I]. |
| **State transitions** | None. |
| **Database** | None on any real database. Scratch databases only, through `apply_dark_protection_schema`, which refuses the account database (`db.py:2711-2721` [V]). |
| **Required tests** | `test_1b` keeps asserting non-registration by identity (renamed functions). A static test asserts that `MIGRATIONS[-1][0] == 11` during Phase 3. |
| **Blocks** | Offline engineering: no (it removes a blocker). Live: **yes**. Registration is a live-activation prerequisite. |

### DD-3: `reason` value list and CHECK (P1-Q4)

| Field | Content |
|---|---|
| **Problem** | `reason` is free text (P:631; `db.py:2576` [V]). Retry eligibility (M-1) and audit need a closed set bound to `kind`. |
| **Options** | (a) Keep free text. (b) The RR list (`fill`, `exit_fill`, `trailing`, `cleanup`, `user_recreate`, `supersede_cleanup`). (c) **A closed list per kind that separates the cancel purposes M-1/M-2 need.** |
| **Selected** [S] | **(c).** <br>• **create:** `fill` (protect a confirmed fill), `user_recreate` (explicit user action after `rejected` / `absent_after_settle` / external removal). <br>• **modify:** `fill_resize` (entry fill grew the position; direction `up`), `exit_fill` (a partial exit shrank it; `down`), `trailing` (ratchet; `tighten`), `attribution_cap` (down to the broker-proven sellable quantity under `broken` attribution, D§6.3; `down`), `user_modify` (any direction). <br>• **cancel:** `position_closed` (broker-proven flat), `supersede_cleanup` (the result of a superseded op, Q1), `duplicate_cleanup` (an app-proven extra GTT, M-2), `exit_prepare` (before or within an exit claim, P§7.3, M-7), `user_cancel` (explicit user). |
| **Reason** | Each value maps to one authorising context, so a static review can tell who may claim it. RR's `cleanup` is split into `position_closed` and `duplicate_cleanup`, because their risks differ (M-2). |
| **State transitions** | Only through DX: the reason bounds the possible effect classes; the effect class is evaluated at the send. |
| **Database** | A CHECK binding kind ↔ reason ↔ direction (§4.1). It is cheap now, because the table is empty. |
| **Required tests** | Every (kind, reason, direction) combination: the allowed ones insert, every other one is refused (a parametrised matrix). A static test asserts that only `protection.py` names the literals. |
| **Blocks** | Offline engineering: **yes, until approved** (S1 DDL). Live: no. |

### DD-4: modify `direction` and the retry CHECK (D:1150, D§4.2)

| Field | Content |
|---|---|
| **Problem** | `attempts ≤ 1` is enforced only for creates (`db.py:2594` [V]). Exposure-increasing modifies need the same bound. D:1150 proposed `direction ∈ (up, down, neutral)`. "Neutral" hides the difference between tightening and loosening a stop. |
| **Options** | (a) D:1150's three values. (b) **Four values, classified by the riskiest component.** (c) Two separate columns (quantity delta, trigger delta). |
| **Selected** [S] | **(b).** `direction ∈ ('up','down','tighten','loosen')`, NOT NULL exactly when `kind='modify'`. The riskiest component decides: <br>• any quantity increase → `up`; <br>• else any trigger or limit move away from the market (a looser stop for a long) → `loosen`; <br>• else any quantity decrease → `down`; <br>• else `tighten`. <br>Retry CHECK: `attempts ≤ 1 OR kind='cancel' OR (kind='modify' AND direction IN ('down','tighten'))`. The DDL makes `attempts > 1` *possible*; DX decides whether it is *allowed* at each send. |
| **Reason** | A single column keeps the retry CHECK simple. Classifying by the riskiest component means a combined change can never be retried because of its harmless part [I]. |
| **State transitions** | `unknown → sending` (D§4.3 row 16) is allowed only for cancel and for modify `down`/`tighten`, and only when DX yields `none` or `redundant` **or, for a modify `down` with reason `attribution_cap` only, `caps_sell_exposure` [R2-1]**. **[R3-1] An `attribution_cap` op is sendable only as `caps_sell_exposure`, never as `none` or `redundant`, even when Q_cap equals the open quantity; its seven conditions are re-proved at every send (DX rev. 3).** |
| **Database** | New column `direction` and two CHECKs (§4.1). |
| **Required tests** | The CHECK matrix (create and modify `up`/`loosen` with `attempts=2` are refused). A combined up+tighten modify is classified `up`. Mutation: classify by first component, not riskiest. |
| **Blocks** | Offline engineering: **yes, until approved** (S1). Live: no. |
| **Rev. 2 note [R2-1]** | The retry CHECK is unchanged. It already allows `attempts > 1` for modify `down`, which includes `attribution_cap`; whether a retry is *allowed* is decided at each send by DX, now including `caps_sell_exposure`. Rev. 1 had no conflict in the DDL itself. The conflict was between DX's rule and §4.4, and R2-1 resolves it in DX. |
| **Rev. 3 note [R3-1]** | The retry CHECK is still unchanged. R3-1 changes only how DX classifies an `attribution_cap` modify `down`: step 2 of the rev. 3 precedence runs before `none`, so a retry of such an op (row 16) needs `caps_sell_exposure` re-proved against the retry's own read. If any condition fails, the op is `reduces_protection` and stays `unknown`. |

### DD-5: transition enforcement (P1-Q6) and op↔position consistency (P1-Q5)

| Field | Content |
|---|---|
| **Problem** | The schema does not stop `confirmed → intent`, or an op whose `account_id`/`mode` differ from its position's (Phase 1 §8 [V]). |
| **Options** | (a) SQLite triggers for transitions and consistency. (b) **CAS predicates under `fenced_transaction`, from one allowed-transition table; consistency by construction.** (c) Both. |
| **Selected** [S] | **(b).** <br>• `backend/protection.py` holds one `ALLOWED_TRANSITIONS` table (§4.2). Every state change is one `UPDATE … WHERE id=? AND op_uuid=? AND state IN (<allowed from>) AND <predicates>` inside `instance_lock.fenced_transaction`. Rowcount 0 means refused, and nothing is sent. <br>• **The claim is `INSERT INTO protection_operations (account_id, mode, position_id, …) SELECT t.account_id, t.mode, t.id, … FROM trade_tickets t WHERE t.id = ? AND t.mode IS NOT NULL AND <claim predicates>`**, so `account_id` and `mode` cannot differ from the position's. A ticket with `mode IS NULL` (allowed by `trade_tickets`, `db.py:102` [V]) cannot be journaled. <br>• A static test asserts that no module other than `protection.py` (and `db.py`'s DDL) writes the table. <br>• No triggers in Phase 3; revisited at registration (DD-2). |
| **Reason** | Triggers would add objects to `dark_protection_schema_status` and to the byte-identical rollback check. They also enlarge `ticket_events`-style rebuild hazards (`_rebuild_ticket_events` refuses tables with triggers, `db.py:2786-2788` [V]). Fenced CAS is already the Phase 2 discipline (`instance_lock.py:700-729` [V]). |
| **State transitions** | Every transition in §4.2 is a CAS. Terminal states have no outgoing CAS (row 23 changes no op state). |
| **Database** | No triggers. The claim shape is `INSERT … SELECT`. |
| **Required tests** | (1) Every pair outside `ALLOWED_TRANSITIONS` is refused (generated matrix). (2) A claim on a ticket with `mode NULL` is refused. (3) A claim with a foreign `account_id` is impossible: no such parameter exists. (4) Static single-writer test. (5) Mutation: remove the `state IN` predicate from one CAS. |
| **Blocks** | Offline engineering: **yes, until approved** (S4 design). Live: no. |
| **Rev. 2 [R2-4]: `mode` values and mapping, VERIFIED from source** | **Verified facts** [V]: <br>1. `trade_tickets.mode TEXT CHECK (mode IN ('dry_run', 'live') OR mode IS NULL)` (`db.py:102`). Both table rebuilds carry the identical CHECK (`db.py:590`, `db.py:1452`). No `COLLATE` clause is declared, so SQLite's default BINARY comparison applies: `'LIVE'`, `' live'` and `'paper'` cannot be stored. <br>2. A new BUY ticket is inserted **without** `mode`, so the column is NULL until the claim: the manual route inserts status `draft` with no `mode` column (`routes/tickets.py:239`), and so does the strategy entry (`strategy_engine.py:386`). <br>3. The claim stamps it: `_transition(…, mode=mode)` (`routes/tickets.py:1166-1167`), with `mode` from `_claim_mode` (`:719-749`). A new entry takes the global `execution_settings.mode`; an exit takes its position's mode. `_require_valid_mode` (`:752-764`) refuses anything else with a 409 before the claim. It delegates to `paper_broker.is_valid_mode` (`paper_broker.py:121, 129-130`: `type(mode) is str and mode in VALID_MODES`, with `VALID_MODES = ("dry_run", "live")`). <br>4. Exit rows inherit their position's mode when they are created: a linked manual full close (`routes/tickets.py:359-365`); a strategy exit (`strategy_engine.py:477`, "an exit inherits the position's mode, never the global one (PK-3)"); the Emergency Exit ticket (`routes/kill_switch.py:573`, `mode` column); the GTT-trigger exit record (`trailing_engine.py:669`, `ticket["mode"]`). An unlinked reduction stays NULL and takes the global mode at its claim (`routes/tickets.py:359-361`). <br>5. **There is no `paper` mode.** `dry_run` is the mode; the paper broker is its broker. Exactly `dry_run` routes to the paper broker and exactly `live` to Kite; anything else refuses every broker write (`paper_broker.py:110-118`). <br>6. `execution_settings.mode` (`db.py:143`: NOT NULL, default `dry_run`, same two values) is the **global** switch. It is a different column from the ticket's mode. <br>7. The dark `protection_operations.mode` is `TEXT NOT NULL CHECK (mode IN ('live', 'dry_run'))` (`db.py:2574`): the same two-value set as the ticket's non-NULL domain. <br>**Mapping** [S], unchanged in shape: the DD-5 `INSERT … SELECT` copies `trade_tickets.mode` **verbatim**. The mapping is the identity on `dry_run` and `live`. A NULL ticket mode yields no row (claim predicate `t.mode IS NOT NULL`, with the column's NOT NULL CHECK as backstop). The op's mode is **never** taken from `execution_settings.mode`. Each send selects its broker from the op's own `mode` through `paper_broker.check_mode` (G7; existing call sites `execution.py:333`, `routes/kill_switch.py:74` [V]). <br>**UNVERIFIED:** whether any real database holds an **open executed BUY position whose `mode` is NULL** (a legacy row). The code anticipates the case: an exit of a NULL-mode position is refused with `EXIT_MODE_UNRESOLVED` (`routes/tickets.py:733-737` [V]). It was not checked, because the account database is out of bounds. The fail-closed posture: such a position cannot be journaled, so the engine never protects it automatically. The registration preflight (DD-6, amended) must count such rows and report zero. <br>**Related fact, not part of the mapping** [V]: `risk_engine._OPEN_POSITION_WHERE` counts only `mode = 'live'` positions (`risk_engine.py:396-400`). This matters for which positions the Phase 4+ wiring scans. <br>**Decision status:** resolved. The facts are verified, the decision is unchanged, and the one data fact is UNVERIFIED with a fail-closed posture and a registration check. **Added test:** a claim copies `dry_run` and `live` verbatim, and a parametrised claim on NULL, `'LIVE'` and `'paper'` tickets yields no row (the latter two cannot even be stored; asserted at the DDL). |

### DD-6: duplicate `kite_gtt_id` rows and `idx_trade_tickets_unique_gtt`

| Field | Content |
|---|---|
| **Problem** | `_ensure_unique_gtt_index` skips the index when existing rows share a `kite_gtt_id` (`db.py:2819-2835` [V]). Once v12 is registered, `migrate()` never re-runs a recorded version, so a skip would be permanent [I, RR§7.3]. The engine relies on "a GTT protects at most one ticket" for adoption and linking. |
| **Options** | (a) Skip and report forever. (b) Refuse the migration if duplicates exist. (c) Skip, raise an URGENT finding, and re-check at startup. (d) **Dark phase: keep (a). At registration: a read-only preflight plus (b), and an engine-level guard that is independent of the index.** |
| **Selected** [S] | **(d).** <br>• **Dark (Phase 3):** unchanged skip + warn + report (Phase 1 test `10h` stays). <br>• **At registration:** <br>&nbsp;&nbsp;1. a read-only duplicate check, run on the operator backup (`mode=ro`), must report zero; <br>&nbsp;&nbsp;2. the registered `_m012` raises `DatabaseMigrationFailed` if duplicates exist. Nothing changes: the database stays at v11, and the v11 build accepts it. <br>• **Always:** every engine link CAS adds `AND NOT EXISTS (SELECT 1 FROM trade_tickets WHERE kite_gtt_id = :gtt AND id != :pos)`. |
| **Reason** | (c) leaves the invariant unenforced while the engine runs. (b) without a preflight could stop the app at upgrade time. The preflight makes (b) a should-never-fire backstop. The engine guard makes correctness independent of whether the index exists [I]. |
| **State transitions** | A link that would duplicate an id fails its CAS. The op goes to `needs_review` (contradictory evidence: "id linked elsewhere", D§4.3 row 12/18). |
| **Database** | Dark: none. Registered `_m012`: the refusal branch (written in S2, active only once registered). |
| **Required tests** | Dark: `10h` unchanged. S2: the registered-mode refusal leaves the database at v11, byte-identical. The preflight reports duplicates read-only. Engine: a link CAS with the index absent and a duplicate present is refused → `needs_review`. |
| **Blocks** | Offline engineering: no. Live: **yes**: the preflight is part of the registration gate. |
| **Rev. 2 amendment [R2-4]** | The read-only registration preflight also counts **open executed BUY tickets with `mode IS NULL`**. The count must be zero; otherwise registration waits for a human to resolve those rows. Such a position cannot be journaled (DD-5), so the engine could never protect it. Test: the preflight reports a NULL-mode open position read-only and changes nothing. |

### DD-7: `protection_state` values, `updated_at`, event types (P1-Q7, P1-Q9)

| Field | Content |
|---|---|
| **Problem** | D refers to a position state `CLOSED_CLEANUP_REQUIRED` (D:278, D:474), but `PROTECTION_STATES` does not contain it (`db.py:2547-2552` [V]). The journal has no `updated_at`. Every new event type means another `ticket_events` rebuild, which is cheap only before registration. |
| **Options** | (a) Add `CLOSED_CLEANUP_REQUIRED` to `protection_state`. (b) A separate stored column `position_cleanup_state`. (c) **Derive it** from persisted facts. Events: add the two D:1152 types now, or **fix the complete list now**. |
| **Selected** [S] | <br>• **(c)**: `CLOSED_CLEANUP_REQUIRED` is **derived, never stored**: the position is broker-proven flat (`closed_at` set, open quantity 0), **and** either some op on it is open (any kind), or its linked `kite_gtt_id` is not confirmed `deleted`/`cancelled`/consumed by its own trigger fill. `derive_restriction` maps it to `CLEANUP_REQUIRED`. <br>• **`updated_at TEXT NOT NULL DEFAULT (datetime('now'))`** on `protection_operations`; every CAS sets it. <br>• **Event types fixed at 13**: the existing six (`protection_pending`, `_submitted`, `_confirmed`, `_unknown`, `_adopted`, `_cancelled`) plus `protection_rejected`, `protection_not_sent`, `protection_absent_after_settle`, `protection_needs_review`, `protection_superseded`, `protection_resolved`, `protection_late_gtt_appeared`. |
| **Reason** | A stored copy of a derivable fact drifts, and a drift here would either hide cleanup or block entries forever. The `protection_state` column already carries `PROTECTION_CANCEL_PENDING`/`_UNKNOWN` (P§3.3). One event per reachable outcome gives a complete audit trail with one rebuild [I]. |
| **State transitions** | None on ops. Every op transition appends the matching event in the same fenced transaction. |
| **Database** | No change to `PROTECTION_STATES`. `updated_at` is added. The `ticket_events` suffix becomes the 13-type list (`_PROTECTION_EVENT_TYPES_SUFFIX`). Rollback removes exactly that suffix. Note: `protection_failed` already exists in `ticket_events` and is not part of the suffix (Phase 1 §5.3 [V]). |
| **Required tests** | The derived cleanup state for each case: flat with an open cancel; flat with a linked GTT unconfirmed; flat and confirmed (not cleanup). The event-suffix rebuild is byte-identical on rollback (Phase 1 test 8 pattern). Every transition writes exactly one event (an engine invariant test). |
| **Blocks** | Offline engineering: **yes, until approved** (S1 rebuild content). Live: no. |
| **Rev. 2 amendment [R2-6]** | The derivation of `CLOSED_CLEANUP_REQUIRED` counts a GTT id's cleanup obligation as discharged in one more case. It is discharged when the id is confirmed `deleted`/`cancelled`/consumed by its own trigger fill (rev. 1), **or** when a manual resolution of an op targeting or producing it recorded `no_app_gtt` or `deleted_in_kite_verified` (Q3) with a complete evidence read. Discharge by `no_app_gtt` arms the late-match watch for that id for `LATE_MATCH_WATCH_DAYS`. If the id is later listed `active` → `LATE_GTT_APPEARED` (URGENT), and an automatic `position_closed` cancel is claimed (`none`, because the position is flat). **Why:** without this case, a human resolution on a flat position could never clear the derived cleanup state, and the position would block forever. **Added test:** a flat position whose only obligation is resolved `no_app_gtt` is not in cleanup, and a later `active` listing of that id raises the finding and claims the cancel. |

### DD-8: settle, watch and freshness parameters

| Field | Content |
|---|---|
| **Problem** | The settle rule, late-match watch and pre-write freshness need values. The real ones depend on P§10 items 9–12, which are [U]. |
| **Options** | (a) Wait for broker evidence. (b) **Config placeholders at the D/P values, marked pending.** (c) Make the values user-configurable now. |
| **Selected** [S] | **(b)**, in `backend/config.py` at S4, each commented `PENDING P§10 items 9–12 (Phase 9)`: <br>• `PROTECTION_SETTLE_SECONDS = 120` (P:444); <br>• `SETTLE_MIN_COMPLETE_READS = 2`, `SETTLE_READ_SPACING_SECONDS = 60` (D:328); <br>• `LATE_MATCH_WATCH_DAYS = 30` (D:338); <br>• `PREWRITE_MAX_AGE_SECONDS = 15` (D:661); <br>• `FILL_VISIBILITY_TOLERANCE_SECONDS = 60` (existing, `config.py:225` [V]); <br>• `PROTECTION_INFLIGHT_GRACE_SECONDS = <SDK timeout> + 5` (D§4.3 row 9); <br>• `PROTECTION_RETRY_BACKOFF = 1, 2, 4 … ≤ 300 s`, `PROTECTION_RETRY_MAX_ATTEMPTS = 10` (D:445). <br>**Freshness rule:** a read is usable for a send only if (i) `broker_reads.complete = 1`; (ii) its **wall-clock** age ≤ the limit; (iii) it **started after the last resume and the current takeover**. The monotonic clock is never used alone for read age. |
| **Reason** | Fail-closed engineering needs only a defined value. If `time.monotonic()` does not advance across sleep (true on some hardware; U3 was answered for one laptop only, WIN §3.4 via RR§11.1), a pre-suspend read could look fresh by monotonic age [I]. Rule (iii) closes that. |
| **State transitions** | Row 17 (settle) uses the count and spacing. Rows 5/16 use the freshness rule. |
| **Database** | None (config). Settle evidence (the `broker_reads` ids) is recorded in `evidence`. |
| **Required tests** | Settle: failed, partial or malformed reads increment nothing; reads < 60 s apart count once; the first counted read is after `max(sent_at + SETTLE, recovery start)`. Freshness: a read taken before an injected resume is refused even with a small monotonic age. |
| **Blocks** | Offline engineering: no (placeholders suffice). Live: **yes**: the real values need Phase 9 evidence (B-08, B-10, B-11). |
| **Rev. 2 clarification [R2-5]: earliest `absent_after_settle`** | **Timestamps used** [V for the columns]: `broker_reads.started_at` is stamped before the first endpoint call (`db.py:2132`). `broker_reads.gtts_at` is stamped **after** `get_gtts()` returns, so it is a completion time (`startup_reconciliation.py:348`; also stamped after a failed call, `:340`, `:344`), and `finished_at` closes the read. All three are wall-clock values. <br>**Settle rule, made exact** [S]. A read R counts toward settle only if it is complete, shows no fingerprint match and no unlinked active SELL GTT, and: <br>(a) `R.started_at ≥ T0`, where **T0 = max(`sent_at` + `PROTECTION_SETTLE_SECONDS`, `unknown_at`, `reset_at`)**. Here `unknown_at` is when the op entered `unknown` (recorded in `evidence`), and `reset_at` is the latest of recovery start, the last resume and the last epoch change (G4 resets the count); <br>(b) for the second counted read R2 after the first R1: **`R2.started_at − R1.gtts_at ≥ SETTLE_READ_SPACING_SECONDS`**. Measuring from R1's `get_gtts` *completion* to R2's read *start* guarantees that the two `get_gtts` calls began at least 60 s apart, whatever the endpoint ordering inside each read. <br>**Earliest time** (`SETTLE_MIN_COMPLETE_READS = 2`): <br>&nbsp;&nbsp;T_abs ≥ R2.`finished_at` ≥ R2.`started_at` + d2 ≥ R1.`gtts_at` + 60 + d2 ≥ T0 + g1 + 60 + d2 <br>Here g1 is R1's time from `started_at` to its `get_gtts` completion, and d2 is R2's full duration. <br>**With the placeholders**, no restart and no resume: `unknown_at` ≤ `sent_at` + `PROTECTION_INFLIGHT_GRACE_SECONDS` (7 + 5 = 12 s) < `sent_at` + 120, so T0 = `sent_at` + 120 s. Hence **T_abs ≥ `sent_at` + 180 s + g1 + d2**: never earlier than **3 minutes** after the send, plus the two read latencies. In practice the bound is later, depending on the cadence of the loop that takes the reads [I]. <br>**Examples:** <br>• restart with recovery start S > `sent_at` + 120 → T_abs ≥ S + 60 + g1 + d2; <br>• a resume at X after R1 was counted → the count resets, so T_abs ≥ max(`sent_at` + 120, X) + 60 + g1 + d2. <br>**Static check** [S]: `PROTECTION_SETTLE_SECONDS ≥ PROTECTION_INFLIGHT_GRACE_SECONDS`. Otherwise a read taken while the op may still be in flight could count, and rule (a)'s `unknown_at` term would become the binding one. <br>**Clock residual** [I]: the timestamps are wall-clock and comparable across processes. A backward step makes reads look *closer*, so it fails safe. A forward step (an NTP correction) could make reads look further apart than they were. Within one process the engine also records `time.monotonic()` for each counted read in `evidence`, and requires both differences ≥ 60 s. Across a restart only wall clock is available, which is disclosed. Timestamps are compared as parsed instants, never as strings (`datetime('now')` and ISO formats differ). <br>**Added tests:** reads exactly at the bound count; 1 s earlier they do not. An R1 whose orders call is slow does not shorten the `get_gtts` spacing. A resume between R1 and R2 restarts the count. A forward wall-clock step within one process does not satisfy the spacing. The static `SETTLE ≥ GRACE` check. |

### DD-9: the predicate for automated exposure-increasing sends

| Field | Content |
|---|---|
| **Problem** | `derive_restriction` adds `UNRESOLVED_OPERATION` for any open op (`startup_reconciliation.py:153`, in `local_reasons`; Ph2§7 [V]). Requiring `NORMAL` would stop the engine from ever recovering its own ops. Not requiring anything would let creates run pre-reconciliation. |
| **Options** | (a) Require `NORMAL`. (b) No gate beyond authority. (c) **A dedicated predicate `may_increase_exposure(position)`.** |
| **Selected** [S] | **(c).** Required for every `increases_sell_exposure` claim and send (create, modify `up`). All of these must hold: <br>1. the process is the owner and its epoch is current (checked by the fenced CAS); <br>2. a Stage 1 run `passed` in this epoch, started after the last login **and** after the last resume; <br>3. a complete read satisfying the DD-8 freshness rule; <br>4. none of these account reasons is set: `INSTANCE_*`, `HEARTBEAT_STALE`, `RESUMED_AFTER_SUSPEND`, `NO_SESSION`, `PROXY_UNAVAILABLE`, `BROKER_READ_FAILED`, `BROKER_READ_STALE`, `BROKER_DATA_INCONSISTENT`, `LATE_GTT_APPEARED`; <br>5. attribution for the instrument is `exclusive`, or `shared` (create only up to the app quantity), per D§6.3; <br>6. the M-6 clock-gap check passes inside the CAS; <br>7. the P§5.3 "provably not created" evidence holds (M-5). <br>**Deliberately excluded:** `UNRESOLVED_OPERATION`, `PROTECTION_GAP`, `CLEANUP_REQUIRED`, `RECONCILIATION_REQUIRED` *for other positions*. For the position itself, the indexes and claim predicates (no open op; no exit in flight) already govern. |
| **Reason** | It keeps every condition that says "the app cannot see the broker reliably", and drops only the conditions the engine exists to fix. |
| **State transitions** | A failed predicate at claim: no op. At send: `intent → not_sent`. |
| **Database** | None (read-only predicate over v11 tables and the journal). |
| **Required tests** | Each condition 1–7 failing alone refuses (parametrised). `UNRESOLVED_OPERATION` on another position does not refuse. Mutation: replace the predicate with `NORMAL`; recovery must then deadlock, and a test detects it. |
| **Blocks** | Offline engineering: **yes, until approved** (S4/S8). Live: no additional blocker. |

### DD-10: G1 resume guard, G2 late response after authority loss, G4 cross-cycle memory

| Field | Content |
|---|---|
| **Problem** | RR§10: <br>• **G1:** a loop cycle or route can act after a wake before the heartbeat sees the gap; <br>• **G2:** a response arriving after authority loss cannot be recorded (`fenced_transaction` raises, `instance_lock.py:717-721` [V]); <br>• **G4:** `_price_seen`/`_reverse_seen` survive a resume, so one pre-suspend and one post-resume sighting can confirm a two-cycle finding (`reconciliation.py:75-103` [V]). |
| **Options** | For each: adopt in Phase 3, defer, or reject. |
| **Selected** [S] | **Adopt all three in Phase 3.** <br>• **G1:** see M-6. <br>• **G2:** the engine writes one CRITICAL structured log line, `event=protection.response_after_authority_loss op_id op_uuid attempt epoch kind outcome result_gtt_id fingerprint`, and **no database write**. The successor resolves by reads (adopt by fingerprint, or settle). <br>• **G4:** ~~`startup_reconciliation.note_resume` calls `reconciliation.reset_cycle_memory()`.~~ **[R2-7] The reset is consumer-driven (rev. 2 row below).** The engine resets `complete_reads_after` to 0 for every `unknown` create, on a resume or an epoch change. |
| **Reason** | G2: writing after the fence would defeat fencing; the log is the human's reconciliation aid. G4: a post-resume confirmation must rest on post-resume sightings [I]. The settle reset is required for the same reason: a request in flight across a suspend can land after it. |
| **State transitions** | G2: none (the op stays as the successor finds it: older-epoch `sending` → `unknown`, row 10). G4: settle counts restart. |
| **Database** | None. |
| **Required tests** | G2: an E2-style subprocess test. FakeKite `on_call` holds `place_gtt`; the epoch is bumped; the response returns; there is no row change and the log line is present; the successor adopts. G4: a one pre-suspend plus one post-resume sighting does not confirm; settle count reset after a simulated resume. Mutations: remove the G4 reset call; let G2 write to the DB. |
| **Blocks** | Offline engineering: **yes, until approved** (S5/S8). Live: G1's legacy-path extension (M-6) is a live blocker. |
| **Rev. 2 amendment [R2-7]: where the G4 reset runs** | ~~`startup_reconciliation.note_resume` calls `reconciliation.reset_cycle_memory()`.~~ **Replaced:** `instance_lock` keeps a monotonically increasing `resume_seq` (M-6). The reconciliation classification entry points compare it with the value they last saw, and call `reset_cycle_memory()` themselves on a change, **while holding `CLASSIFICATION_LOCK`**. These are `_run_cycle`, which already runs under that lock (`@_one_at_a_time`, `reconciliation.py:95, 186-187` [V]), and Stage 1's classification block (`startup_reconciliation.py:585` [V]). The settle-count reset of `unknown` creates is keyed the same way. **Why:** `reset_cycle_memory()` clears module dicts without taking the lock (`reconciliation.py:99-103` [V]). Calling it from the event loop or from an engine CAS either races a running cycle, or must take `CLASSIFICATION_LOCK` from a context that holds, or blocks, other locks (M-6, R2-7). A consumer that resets on seeing a new `resume_seq` cannot miss a resume and needs no cross-thread call. **Added test:** a cycle that starts after a resume detected only by an engine CAS (the heartbeat has not yet run) still sees fresh memory. |

### DD-11: meaning of "dry run" in Phase 3

| Field | Content |
|---|---|
| **Problem** | P:1224 says the engine is "invoked only by tests and for dry run". An in-app dry run would need the journal on the real database. |
| **Options** | (a) **Tests only.** (b) Also dry-run tickets in the running app. |
| **Selected** [S] | **(a).** `mode='dry_run'` ops exist only in tests on scratch databases (paper-parity tests). |
| **Reason** | (b) needs DD-2 registration and touches `routes/tickets`, a live path out of Phase 3 scope (RR§14 item 5). |
| **State transitions** | None. |
| **Database** | None. |
| **Required tests** | Paper parity: the same transition sequences in `live` and `dry_run` mode on FakeKite and on the paper broker. |
| **Blocks** | Offline engineering: no. Live: no. |

### Q1: `superseded` overlay sub-choices (RR§6.2)

| Field | Content |
|---|---|
| **Problem** | Three choices are left open: (a) which actors map to `superseded_by_kind`; (b) whether `protection_superseded` is added now; (c) what a superseded op's resolution does when the superseding exit did **not** close the position. Point (c) is new: D:274-281 always claims a cancel, which can remove the only protection of a still-open position (M-2). |
| **Options** | For (c): (i) always cancel (D); (ii) clear the overlay when the exit fails; (iii) **keep the overlay as a historical fact and re-derive the action from the position state at resolution time.** |
| **Selected** [S] | (a) `superseded_by_kind`: <br>• `emergency_exit`: an Emergency Exit claimed the exit owner; <br>• `exit`: a manual or strategy exit claim (Phase 6); <br>• `close`: the position became flat by a broker fact the app did not initiate as an exit claim (a GTT trigger fill, an external sale detected), via `close_if_flat`; <br>• `user`: an explicit "stop protecting" user action. <br>`superseded_by_ref` is the exit ticket id or the `system_events` id. <br>(b) **Yes, now** (DD-7). <br>(c) **(iii).** The resolution-time action table is in §4.3. In short, when the resolution reveals a GTT id: <br>• position broker-flat → claim a `supersede_cleanup` cancel (`none`, automatic); <br>• exit in flight → claim a `supersede_cleanup` cancel (`reduces_protection`, allowed inside the exit claim, one attempt; automatic once flat **per the M-1 rev. 2 state table [R2-2]**); <br>• position open and no exit in flight → link the GTT as the position's protection if its shape fits and nothing else is linked; otherwise apply the M-2 rules or go to `needs_review`. |
| **Reason** | Option (ii) mutates audit history. Option (i) violates M-2. Option (iii) keeps the record and makes the action depend on facts at the moment of action [I]. |
| **State transitions** | D§4.3 row 20 is replaced by the §4.3 table. The overlay never changes the op state by itself (row 19). |
| **Database** | The overlay triplet and its all-or-none CHECK; the event type. No "lapsed" column: the lapse is recorded in `evidence` plus the `protection_superseded` and `protection_confirmed`/`_adopted` events. |
| **Required tests** | The four rows of §4.3, each in both orders (supersede before or after the `sending` commit). An exit that fails after the supersede, followed by a late ack: the GTT is linked, not cancelled. Pre-send CAS race: supersede vs `intent→sending`, both orders. |
| **Blocks** | Offline engineering: **yes, until approved** (S7). Live: Phase 6 wiring is a live blocker (M-7). |

### Q2: `absent_after_settle` sub-choices (RR§6.2)

| Field | Content |
|---|---|
| **Problem** | Three choices are left open: (a) the parameters (→ DD-8); (b) a resume between settle reads (→ DD-10 G4); (c) whether the CHECK requires `kind='create'` (→ M-8). |
| **Options** | As in DD-8, DD-10 and M-8. |
| **Selected** [S] | (a) DD-8 placeholders. (b) Reset the count on a resume or an epoch change. (c) **Yes**: CHECK `state != 'absent_after_settle' OR (kind='create' AND sent_at IS NOT NULL AND complete_reads_after >= 2)`. Read spacing and post-recovery ordering cannot be expressed in a CHECK. The engine enforces them and records the `broker_reads` ids in `evidence`. |
| **Reason** | See M-8. |
| **State transitions** | Row 17 is create-only. Modify/cancel with the target missing follow M-3. |
| **Database** | The CHECK above. |
| **Required tests** | The CHECK refuses a modify or cancel in `absent_after_settle`. The settle tests of DD-8. The evidence lists ≥ 2 distinct complete read ids ≥ 60 s apart. |
| **Blocks** | Offline engineering: **yes, until approved** (S1). Live: the parameter values (DD-8). |
| **Rev. 2 note [R2-5]** | (a) and (b) now follow the exact settle rule in DD-8 rev. 2: T0, the spacing measured from R1's `get_gtts` completion to R2's start, and an earliest time of `sent_at` + 180 s plus read latencies with the placeholders. The CHECK in (c) is unchanged (`complete_reads_after >= 2`). Timing cannot be expressed in a CHECK, so the engine records both read ids, their `started_at`/`gtts_at` and the in-process monotonic values in `evidence`. |

### Q3: `needs_review` resolution sub-choices (RR§6.2)

| Field | Content |
|---|---|
| **Problem** | Four choices are left open: (a) the resolution `outcome` enum (D:1148 includes `other`); (b) whether `resolved_manual` is reachable from `unknown`; (c) `direction` (→ DD-4); (d) the restriction gate (→ DD-9). |
| **Options** | (a): keep `other`, or **drop it**. (b): allow it from `unknown`, or **only from `needs_review`**. |
| **Selected** [S] | (a) `outcome ∈ ('linked_gtt','no_app_gtt','deleted_in_kite_verified')`. **`other` is dropped.** `evidence_read_id NOT NULL` (FK `broker_reads`); the engine requires `complete=1` and a read started at resolution time. `(outcome='linked_gtt') = (linked_gtt_id IS NOT NULL)`. `op_id UNIQUE`. `reason` and `resolver` are non-blank. <br>(b) **Only from `needs_review`.** `unknown` clears only by reads. A human who believes an `unknown` is stuck has nothing to resolve against until a read succeeds, and a successful read resolves it automatically or escalates it to `needs_review` (row 18). |
| **Reason** | `other` with free text is an evidence-free exit from the strongest safety state. Allowing a human to close `unknown` would bypass the settle rule. |
| **State transitions** | Row 22 only (`needs_review → resolved_manual`). |
| **Database** | The `protection_operation_resolutions` DDL in §4.1. |
| **Required tests** | A resolution without a fresh complete read is refused. `linked_gtt` without an id is refused, and an id with another outcome is refused. A second resolution of the same op is refused. A resolution from `unknown` is refused (CAS). The linked id must be active and match the shape in that read. |
| **Blocks** | Offline engineering: **yes, until approved** (S1/S6). Live: the UI and route are Phase 8. |
| **Rev. 2 clarification [R2-6]: resulting position protection state** | **Rule** [S]: **`resolved_manual` describes the op, never the position. It never sets `PROTECTION_CONFIRMED` by itself.** In the resolution's fenced transaction the engine applies the action below, then **re-derives** the position's `protection_state` from the resolution's evidence read. The P§3.3 definitions apply: `PROTECTION_CONFIRMED` requires the linked GTT `active` on the instrument, every leg a SELL of exactly the open quantity, and trigger and limit as intended (P:256). Any other open op on the position still applies, under the P:339 precedence. <br><br>**Position open:** <br>• **`linked_gtt`, create or modify op.** Action: link the id as `kite_gtt_id` (DD-6 guard). Resulting state, from the evidence read: `PROTECTION_CONFIRMED` only if the full P:256 shape holds; legs ≠ open quantity → `PROTECTION_RESIZE_PENDING`; any other fault → `PROTECTION_MISMATCH`. On day 0, a confirmed state is still shown as "protective value unverified" until B-03/B-05 are answered. <br>• **`linked_gtt`, cancel op.** The human found the target still `active` and keeps it as the stop, so the cancel did not apply. Action: the target stays (or becomes) the linked id. Resulting state: derived exactly as in the previous case. **Protected** only if P:256 holds. <br>• **`no_app_gtt`, any kind.** Action: no GTT is linked from this op. A linked id that was this op's target is **unlinked**, by human decision, recorded in `evidence` and in the event (M-5 forbids only *automatic* unlinking). Resulting state: **`PROTECTION_FAILED`** (unprotected) unless another linked GTT remains, in which case the state is derived from that GTT. The next create needs **`user_recreate`** (M-5 cond. 3, amended). **Not protected.** <br>• **`deleted_in_kite_verified`, any kind.** Action: as for `no_app_gtt`. The read must show an explicit `deleted`/`cancelled` status (B-32). For a cancel op, this is the cancel's goal. Resulting state: **`PROTECTION_FAILED`** unless another linked GTT remains. **Not protected.** <br><br>**Position broker-proven flat:** <br>• **`linked_gtt`, any kind.** Action: record the id, then claim a `position_closed` cancel automatically (`none`). Resulting state: derived `CLOSED_CLEANUP_REQUIRED` until that cancel is confirmed. Protection: n/a (no shares). <br>• **`no_app_gtt` or `deleted_in_kite_verified`.** Action: the cleanup obligation for that id is discharged (DD-7 rev. 2); after `no_app_gtt`, the late-match watch is armed. Resulting state: `PROTECTION_NOT_REQUIRED` if nothing else is open (P:251). Protection: n/a. <br><br>**Why `PROTECTION_FAILED`, not `PROTECTION_PENDING`:** `PENDING` invites an automatic create (P:252). Once a human has taken part, re-creating protection is the human's call, as after an external deletion (M-5). **Added tests:** each outcome × kind × open/flat row above. No outcome yields `PROTECTION_CONFIRMED` when the evidence read shows a quantity or trigger mismatch. Mutation: map `resolved_manual` to `PROTECTION_CONFIRMED`. |

### M-1: cancellation is not automatically exposure-reducing

| Field | Content |
|---|---|
| **Problem** | D§4.2 (D:446) and D§8.4 (D:943) treat every cancel as safe to retry automatically. A cancel of an open position's stop increases protection exposure. |
| **Options** | (a) Keep D§4.2. (b) Never automate cancels. (c) **Automate by effect (DX).** |
| **Selected** [S] | **(c).** A cancel is automatic, both claim and retry, only when its effect is `none` (position broker-flat) or `redundant` (M-2). Otherwise: <br>• `exit_prepare` sends once inside the exit claim; <br>• `user_cancel` sends once at the user's request; <br>• neither is retried automatically while the position holds shares. A failed or unknown one is surfaced, and the exit or the user acts again. <br>~~A cancel whose effect changes to `none` (the position goes flat) becomes automatically retryable on the same op.~~ **[R2-2] Replaced by the state table in the rev. 2 row below:** what happens when the effect becomes `none` depends on the op's state at that moment, and a terminal op is never resent. **Amends D§4.2 and D§8.4** (§6). |
| **Reason** | Removing a stop is a protection decision, and it belongs to an exit or to the user. Once no shares remain, the only remaining risk is the double sell, and then cancelling is purely beneficial. **[R2-1]** The one automated reduction of protection is not a cancel. It is the `caps_sell_exposure` modify (DX rev. 2), which lowers the quantity only to what the broker proves the app may sell and never removes the stop. **[R3-1] Precisely:** the cap reduces protection only when Q_cap is below the open quantity, and then leaves up to (open − Q_cap) app-recorded shares uncovered (position `PROTECTION_NEEDS_REVIEW`). When Q_cap equals the open quantity it reduces only sell exposure, and it is still `caps_sell_exposure`, not `none` (DX rev. 3). |
| **State transitions** | Row 16 (`unknown→sending`) for cancel requires DX ∈ {`none`, `redundant`}. Row 1 for an automatic cancel claim requires the same. |
| **Database** | DD-3 reasons; the DD-4 retry CHECK. |
| **Required tests** | `position_closed` auto-retries. `exit_prepare` on an open position does not auto-retry, ~~and does once flat~~ **and, once flat, follows the rev. 2 state table [R2-2]**. `user_cancel` is never auto-retried. A static test asserts that no code path classifies a cancel by kind. |
| **Blocks** | Offline engineering: **yes, until approved** (S4/S7). Live: no additional blocker. |
| **Rev. 2 amendment [R2-2]: a cancel whose effect becomes `none` (the position is now broker-flat)** | **Why.** M-1 rev. 1 promised a retry "on the same op", while M-3 forbids any automatic retry after `rejected`, and `rejected` is terminal (§4.2 "never allowed"). Both cannot hold. **Selected** [S]: the action depends on the op's state when its effect is re-evaluated as `none`, whatever its original reason (`exit_prepare`, `user_cancel`, `supersede_cleanup`) or class: <br>• **`intent`**: sends normally (row 5). This is a first send, not a retry. <br>• **`sending`**: no action (in flight; rows 9/10). <br>• **`acknowledged`**: no action; the read-back decides (rows 11/15). <br>• **`unknown`**: a **same-op** retry, but only under M-3 conditions 1–3 (row 16): a post-attempt complete read shows the target still `active`; backoff; cap. This is the only case where the rev. 1 "same op" wording holds. <br>• **`needs_review`**: no automatic action. The (position, target) cancel index keeps any second cancel out until the human resolves (Q3). <br>• **`not_sent`** (terminal; nothing left the process): an automatic **new** op, if a fresh complete read shows the target `active`. <br>• **`rejected`** (terminal; a definite broker refusal): an automatic **new** op **only** when the rejection was session-class (`TokenException`, B-30), a new session exists, and a fresh complete read started after that session began shows the target `active` (the M-3 exception). **Any other rejection class → no automatic op**, even though the position is flat. The position stays derived `CLOSED_CLEANUP_REQUIRED` and a finding is raised; a `user_cancel` is needed. A refusal that says the GTT does not exist, while a fresh read lists it `active`, is contradictory evidence: URGENT finding, no re-claim (B-17). <br>• **`confirmed`**: done. <br>• **`resolved_manual`**: per Q3 rev. 2. <br>**A new op** takes its reason from DD-3 at claim time: `supersede_cleanup` if the target is a superseded op's result, otherwise `position_closed`. It is a fresh row (`attempts` from 0). <br>**Bound on re-claims:** across all cancel ops for the same (position, target), `SUM(attempts) ≤ PROTECTION_RETRY_MAX_ATTEMPTS`, and at most one session-class re-claim per broker session. Both are counted from the journal; no new column. <br>**Added tests:** one per state above. A rejected `exit_prepare` (non-session class) on a now-flat position → no automatic op, finding raised. The same with `TokenException` → exactly one new `position_closed` op after re-login. "Does not exist" plus an `active` listing → finding only. The re-claim bound is enforced across ops. Mutation: transition `rejected → sending`. |

### M-2: duplicate protection vs the only valid protective GTT

| Field | Content |
|---|---|
| **Problem** | Two GTTs can protect one position: a user re-create after `absent_after_settle` plus a late appearance; a late ack after a re-create; a superseded op's result. Deleting the wrong one leaves the position unprotected, or deletes the user's own GTT. |
| **Options** | (a) Never auto-delete. (b) Delete the newer one. (c) **Auto-delete only an app-proven duplicate while a verified surviving GTT protects the full open quantity.** |
| **Selected** [S] | **(c).** `duplicate_cleanup` is `redundant`, so automatic, **only if all hold** in one evaluation: <br>1. the target id is **app-proven**: it was returned in a broker response to an app op (recorded `result_gtt_id` from `acknowledged`, or a late response, M-4). An id adopted by fingerprint is inference and does not qualify; <br>2. a **different** GTT is the position's linked `kite_gtt_id`; <br>3. that linked GTT is `active` on the same instrument, every leg a SELL of exactly the open quantity, CNC, trigger and limit as intended, in a complete read that satisfies DD-8 freshness; <br>4. no exit is in flight and no create or modify is open on the position. <br>If any condition fails, the cancel of that target is `reduces_protection` (or of unknown ownership). It is not automatic; the position goes to `NEEDS_REVIEW`. <br>A GTT that appears late after `absent_after_settle` is **never auto-deleted** (D§4.3 row 23 kept). |
| **Reason** | Condition 1 avoids deleting a user's GTT. Condition 3 guarantees that what remains actually protects. Re-checking at the send (DX) covers a fire or deletion of the survivor between claim and send [I]. |
| **State transitions** | Claim and send of `duplicate_cleanup` require the four conditions. If the survivor disappears before the send: `intent → not_sent`, or the op stays `unknown` (no retry). |
| **Database** | Reason `duplicate_cleanup`. Condition 1 needs `result_gtt_id` on the originating op, which already exists. |
| **Required tests** | Each condition failing alone blocks the automation. Survivor deleted between claim and send → no send. An adopted-by-fingerprint id is never auto-deleted. A late GTT after settle is never deleted. Two equally plausible candidates → `needs_review`. |
| **Blocks** | Offline engineering: **yes, until approved** (S6/S7). Live: no additional blocker. |

### M-3: unknown broker outcomes never trigger blind retries

| Field | Content |
|---|---|
| **Problem** | An ambiguous outcome (timeout, 5xx, malformed response, crash) may have been processed (U6M§8 items 3, 5 [U]). Re-sending blindly can duplicate a create, double a resize, or repeat a delete with unknown semantics (U6M§8 item 6). |
| **Options** | (a) Retry after a delay. (b) Never retry anything. (c) **Retry only allowed classes, and only after a read proves the change was not applied.** |
| **Selected** [S] | **(c).** A retry (`unknown → sending`, `attempts + 1`) requires **all** of: <br>1. the kind and direction allow it (DD-4) and DX yields `none`/`redundant` **[R3-1] or, for an `attribution_cap` modify only, `caps_sell_exposure` (aligned with row 16 as amended by R2-1; such an op is never retried as `none`)**; <br>2. a complete read **started after the ambiguous attempt's `sent_at` + grace**, and satisfying DD-8 freshness, shows the target id present in its **expected pre-state** (cancel: still `active`; modify: old legs and trigger); <br>3. the backoff has elapsed and `attempts < PROTECTION_RETRY_MAX_ATTEMPTS`. <br>"No read" means no retry, and so does "read failed or partial". A create, modify `up` or `loosen` is never retried. <br>**Target missing or in another state:** <br>• **cancel, target missing** (not listed): **not** treated as confirmed. After the settle rule → `needs_review` ("listing completeness unverified", B-08/B-28). Revisit after Phase 9; <br>• cancel, target `triggered` → `needs_review`, and the position follows the trigger path (D:777-780); <br>• **modify, target missing / `triggered` / other** → `needs_review`, or `rejected` if the broker's response was a definite refusal; <br>• **cancel or modify, target listed in a status the engine does not know** → `needs_review` (B-33). <br>**After `rejected`** (amends D§4.3 row 7): no automatic retry of any kind, with one exception. A cancel whose effect is `none` and whose rejection was session-class (`TokenException`, C4) may be claimed as a **new op** after a new session exists and a fresh read shows the target still `active`. |
| **Reason** | The only safe evidence that a convergent request did not apply is a later complete read of the target. A missing GTT can mean deleted, expired, triggered and pruned, or not listed (pagination, retention [U]). It is not proof of cancellation. |
| **State transitions** | Row 16 is tightened (conditions 1–3). New rows 15a/15b (§4.2). Row 7 is amended. |
| **Database** | No new column. Each attempt's read id and outcome are appended to `evidence`. |
| **Required tests** | FakeKite `accept_then_raise` on delete followed by a read failure → no retry. The read shows `active` → one retry. The read shows missing → no retry; after settle → `needs_review`. Repeated delete or modify modelled both as a no-op and as an error (G5) → the outcome is the same. Create ambiguity → never re-sent (F1/F2). Mutation: retry without a read-back. |
| **Blocks** | Offline engineering: **yes, until approved** (S5). Live: the cancel-missing semantics may be relaxed only with Phase 9 evidence (B-28). Not a blocker; fail-closed. |
| **Rev. 2 clarification [R2-2]** | The "after `rejected`" exception above remains the **only** automatic path after a rejected cancel. It is now stated precisely: <br>(1) it creates a **new** op and never resends the rejected one; <br>(2) it applies to any rejected cancel whose effect **at the new claim** is `none`, including an `exit_prepare` or `user_cancel` rejected while the position was still open; <br>(3) the fresh read must start after the new session began; <br>(4) it is bounded per (position, target) by the M-1 rev. 2 re-claim bound. <br>A non-session rejection has no automatic follow-up, even on a flat position. M-1's state table (rev. 2) and this decision now say the same thing. |

### M-4: a late broker response is associated with its original operation

| Field | Content |
|---|---|
| **Problem** | A response can arrive after the op moved on: a thread paused across a suspend, a response after the in-flight grace when recovery has already set `unknown`, a retry attempt overtaking an earlier attempt, after a supersede, or after authority loss. `place_gtt` carries no client reference (P§10 item 1 [V], U6M§8 item 4), so association cannot come from broker data. |
| **Options** | (a) Match the response to the op by fingerprint. (b) **Carry an in-process correlation context and gate every response write by a CAS keyed on it.** |
| **Selected** [S] | **(b).** Every broker call runs with an immutable context `(op_id, op_uuid, attempt, instance_epoch, kind, request_fingerprint)`, captured when `sending` is committed. The response handler's write is one fenced CAS `WHERE id=:op_id AND op_uuid=:op_uuid AND attempts=:attempt AND state IN (<expected>)`. <br>• **State `sending`, same attempt** → normal rows 6/7/8. <br>• **State `unknown`, same attempt** (recovery moved it after the grace) → `unknown → acknowledged` (create: link the id with the DD-6 CAS; if the id is linked elsewhere → `needs_review`) or `unknown → rejected` (definite refusal). <br>• **Superseded op** → as above, then the Q1/§4.3 resolution action in the same transaction. <br>• **The attempt is not current** (a later retry was sent) → no transition; the response is appended to `evidence` only, and reads decide. <br>• **Terminal, consistent** (`adopted`/`confirmed` with the same id) → no transition; evidence only. <br>• **Terminal, contradictory** (`adopted` with a different id; `absent_after_settle`, `rejected` or `not_sent` with a returned id) → no op change (terminal). URGENT finding `LATE_GTT_APPEARED` (after settle) or `LATE_RESPONSE_CONTRADICTS` (other cases); position `PROTECTION_NEEDS_REVIEW`; new creates refused on the instrument. <br>• **Authority lost** (`FencingError`) → the G2 log line (DD-10); no write. <br>• **After process death**, no context survives: association is only by fingerprint inference (row 14), labelled as such. |
| **Reason** | `op_uuid` plus `attempt` uniquely identify the request that produced the response within the process. The state predicate makes a stale response unable to overwrite a later fact [I]. |
| **State transitions** | New row 17c (`unknown → acknowledged/rejected` by a late response, same attempt). Late responses for non-current attempts and terminal ops change no state (rows 26/27, §4.2). |
| **Database** | No new column (`op_uuid`, `attempts` exist). Findings use the existing `reconciliation_log`/`log_mismatch` path. |
| **Required tests** | FakeKite `on_call` delays the response past the grace: recovery sets `unknown`, then the response → `acknowledged` with the id linked. Attempt-1 response after attempt 2 was sent → evidence only. Late ack after `absent_after_settle` → finding, op unchanged. Late ack after supersede on a flat position → link plus `supersede_cleanup` cancel in one transaction. After authority loss → G2. Mutation: drop `attempts=:attempt` from the CAS. |
| **Blocks** | Offline engineering: **yes, until approved** (S5). Live: no additional blocker. |

### M-5: a new instance never creates a GTT because the previous one is temporarily absent

| Field | Content |
|---|---|
| **Problem** | After a restart, observations are UNVERIFIED (`gtt_observations.py:1-17` [V via D§2.5]). A read may lag or be incomplete (P§10 item 9 [U]). A naive engine sees "position open, no GTT in the book" and creates a second GTT. |
| **Options** | (a) Wait a fixed time after start. (b) **Creation requires positive "provably not created" evidence; absence is never sufficient.** |
| **Selected** [S] | **(b).** A create is claimable only when the P§5.3 table yields "not created": <br>1. the position has **no** linked `kite_gtt_id`; <br>2. **no** op on it is open, and none is `absent_after_settle` within `LATE_MATCH_WATCH_DAYS` (a create after that needs `user_recreate`); <br>3. its last create, if any, ended `not_sent` (automatic allowed unless superseded) or `rejected` (needs `user_recreate`), **or `resolved_manual` with outcome `no_app_gtt` / `deleted_in_kite_verified` (needs `user_recreate`) [R2-6]**; <br>4. a fresh, post-resume, post-takeover complete read shows **no unlinked active SELL GTT** on the instrument; <br>5. the DD-9 predicate holds. <br>**Linked GTT missing from reads:** never auto-unlink, never create. Protection becomes `PROTECTION_UNKNOWN`; after the settle rule, an URGENT `LINKED_GTT_MISSING` finding. <br>**Linked GTT explicitly `deleted`/`cancelled` by no app op** → `PROTECTION_FAILED` plus `GTT_REMOVED_EXTERNALLY`. Re-create only by `user_recreate`, because the user may have deleted it on purpose. `expired` likewise (B-11). <br>**Older-epoch `sending`** → `unknown` (row 10), settle count reset. |
| **Reason** | Only an app-provable fact (nothing sent, or a definite refusal plus a clean book) can justify a create. Everything else is inference, and inference must not add sell exposure. |
| **State transitions** | Row 1 for `kind='create'` gains conditions 1–5. No transition is driven by "not seen". |
| **Database** | None beyond DD-1. |
| **Required tests** | A restart with FakeKite listing lag (the linked GTT is missing for N reads) → no create, `PROTECTION_UNKNOWN`. An older-epoch `sending` create and an empty book → no create until settle; `absent_after_settle` → still no automatic create. An external delete → `PROTECTION_FAILED`, no automatic create. Failed or partial reads → nothing concluded. A two-process test on a shared FakeKite (S3) shows at most one GTT per position across a takeover. |
| **Blocks** | Offline engineering: **yes, until approved** (S6/S8; needs the S3 shared FakeKite). Live: no additional blocker. |

### M-6: resume from sleep/hibernate immediately gates sensitive operations

| Field | Content |
|---|---|
| **Problem** | Today resume is detected only by the heartbeat, every 15 s (`instance_lock.py:744-756, 802-811` [V]). After wake, loop timers, route requests and the heartbeat can run in any order (RR§10 row 4). |
| **Options** | (a) Rely on the heartbeat. (b) **A synchronous clock-gap check inside every sensitive engine CAS** (G1). (c) (b) plus the same detection in `loop_may_run`, `broker_write` and `confirm_time_blockers`. |
| **Selected** [S] | **(b) in Phase 3; (c) as a separately authorised change that blocks live activation.** <br>• New primitive `instance_lock.clock_gap_check(now, monotonic)`. It compares both clocks with the last heartbeat attempt (`last_hb_wall`/`last_hb_mono`), with the threshold `3 × HEARTBEAT_SECONDS` (the same as `heartbeat_once`). <br>• On a gap it runs the resume path **synchronously and idempotently** (one `resume_seq` per gap): add `RESUMED_AFTER_SUSPEND` to the process reasons, set `resumed_at`, ~~`gtt_observations.reset()`, `reset_cycle_memory()` (G4),~~ and flag `note_resume` (persist plus a Stage 1 request) for the heartbeat loop to run on the event loop. **[R2-7]** The in-CAS part is limited to memory updates under `_state.cv`. `gtt_observations.reset()` runs after the CAS transaction ends, and the G4 reset is consumer-driven through `resume_seq` (DD-10 rev. 2). See the rev. 2 row below. <br>• It is called first inside every engine `→sending` CAS. After a resume, until a Stage 1 run passes that **started after `resumed_at`**: <br>&nbsp;&nbsp;– `increases_sell_exposure`, `reduces_protection`, `redundant` **and `caps_sell_exposure` [R2-1]** sends are refused. The cap's evidence is Stage 1 attribution, which must be recomputed after the resume (`intent→not_sent`, or `unknown` unchanged). **[R3-1] This holds even when Q_cap equals the open quantity: an `attribution_cap` op is never a `none`-effect send (DX rev. 3, step 2 precedes `none`)**; <br>&nbsp;&nbsp;– `none`-effect sends (cleanup of a broker-flat position, trailing tighten) are allowed only with a post-resume read (DD-8 rule iii). This preserves "RESTRICTED never blocks protection upkeep" (D:1243) without trusting pre-suspend observations. <br>• Authority is re-proved by the fenced CAS itself (epoch check), which catches a takeover during sleep through a bypassed lock. <br>• **Legacy paths (live):** extend detection (not refusal) to `loop_may_run`, `broker_write` and `live_preflight.confirm_time_blockers`, so the gap is noticed by whichever code runs first after wake. Refusal applies to exposure-increasing legacy actions: ticket confirmation (already refused by RESTRICTED once the reason is set) and the fill-following resize-up in `sync_protection`. This changes live-path behaviour and needs its own authorisation; it is a live-activation blocker. |
| **Reason** | Detection on the code path that is about to act removes the race. Exposure-reducing upkeep stays available, which is the Phase 2 guarantee. |
| **State transitions** | New row 25: any `→sending` CAS with a gap → refused, resume path run, op unchanged (or `intent → not_sent`). |
| **Database** | None (process state; `note_resume` persists as today). |
| **Required tests** | Injected clocks: a wall-clock gap only; a monotonic gap only; both. A gap below the threshold does nothing. Two concurrent detectors (CAS and heartbeat) produce one `resume_seq` and one `note_resume`. Each send class before and after the post-resume Stage 1. A pre-suspend read is refused for a `none` send. Mutation: remove the check from the CAS. |
| **Blocks** | Offline engineering: **yes, until approved** (S8). Live: **yes**: the legacy-path extension (c) must be authorised and done before activation. |
| **Rev. 2 clarification [R2-7]: concurrency and gating of synchronous detection vs the heartbeat and the engine CAS** | **Verified starting point** [V]: <br>• `_state.cv` is a `threading.Condition` over an `RLock` (`instance_lock.py:139`). <br>• `heartbeat_once` reads **and refreshes** the baseline `last_hb_wall`/`last_hb_mono` inside one `with _state.cv` block, before and regardless of its DB write (`:740-756`). It releases `cv` before `BEGIN IMMEDIATE` (`:766`). <br>• `run_heartbeat_loop` calls `note_resume()` only when **its own** `heartbeat_once` reported `resumed` (`:802-811`). <br>• `fenced_transaction` takes and releases `cv` in `current_identity()`, then runs `BEGIN IMMEDIATE`, then checks the epoch (`:700-721`). <br>• `note_resume` opens **its own connection** and its own `fenced_transaction` (`startup_reconciliation.py:300-308`). <br>• `clear_resume(before)` drops the reason only if `resumed_at ≤ before` (`:823-827`). <br>• Stage 1 takes `CLASSIFICATION_LOCK` **then** the fenced DB transaction (`startup_reconciliation.py:585`), and `_run_cycle` holds `CLASSIFICATION_LOCK` while it writes and commits (`reconciliation.py:186-211`). <br>• `gtt_observations.reset()` takes only the module's leaf `_lock` (`gtt_observations.py:22, 125-127`). <br><br>**Selected behaviour** [S]: <br>1. **One shared baseline, exactly one detection per gap.** `clock_gap_check` reads and refreshes the *same* `last_hb_wall`/`last_hb_mono` in one `cv` critical section, with the same `3 × HEARTBEAT_SECONDS` threshold. Whichever detector enters `cv` first after a wake (the heartbeat, an engine CAS, or a legacy-path hook under (c)) sees the gap. It then, in that same section: increments `resume_seq`; adds `RESUMED_AFTER_SUSPEND`; sets `resumed_at = max(resumed_at, wall)`; and sets `resume_pending`. Every later detector sees a refreshed baseline and no gap. No double resume and no lost resume. <br>2. **The heartbeat must consume a gap it did not detect.** Because of 1, a gap consumed by a CAS is invisible to the next `heartbeat_once`, so the current loop (`:808-811`) would never call `note_resume`. Change (S8): `run_heartbeat_loop` calls `note_resume()` whenever `resume_seq > last_noted_seq`, not only on its own `resumed` result. `note_resume` is idempotent per `resume_seq`. <br>3. **Gating keys on the shared flag, not on the gap.** Every engine send checks `RESUMED_AFTER_SUSPEND ∈ process_reasons()`, plus the "Stage 1 passed and started after `resumed_at`" rule. If the heartbeat detected the gap first, a later CAS sees no clock gap but is still refused by the flag. A second gap during a Stage 1 run moves `resumed_at` past that run's start, so `clear_resume` cannot clear the newer resume (existing code, `:823-827`). <br>4. **Lock order** (no new deadlock): <br>&nbsp;&nbsp;– no code waits for the SQLite write lock while holding `cv`; this is verified true today for `heartbeat_once` and `fenced_transaction`; <br>&nbsp;&nbsp;– `clock_gap_check` may take `cv` **inside** the CAS's `BEGIN IMMEDIATE`, because no `cv` holder ever waits for the DB lock. The order is always DB lock → `cv`, never the reverse [I from the cited code]; <br>&nbsp;&nbsp;– the CAS must **not** take `CLASSIFICATION_LOCK`. Stage 1 holds `CLASSIFICATION_LOCK` while it waits for `BEGIN IMMEDIATE`, so a CAS holding the DB lock and waiting for `CLASSIFICATION_LOCK` would invert the order. Stage 1 would stall until the SQLite busy timeout and then fail [I]. Hence the consumer-driven G4 reset (DD-10 rev. 2); <br>&nbsp;&nbsp;– the CAS must **not** call `note_resume`. On its own connection, `note_resume`'s `BEGIN IMMEDIATE` would wait on the CAS's own write lock until the busy timeout [I]. Hence the deferral to the heartbeat loop (point 2); <br>&nbsp;&nbsp;– `gtt_observations.reset()` (a leaf lock, no I/O) runs after the CAS transaction ends. <br>5. **What the detecting CAS writes.** In the same transaction it applies row 25: `intent → not_sent`, or no change for `unknown`. It then commits and returns. No broker call follows. <br>6. **Epoch before clock.** `fenced_transaction` proves the epoch before the CAS body runs, so a takeover during sleep raises `FencingError` first: no write, no send, authority lost (`lose_authority`). The resume path is then moot for this process. <br>7. **The heartbeat and the CAS share only the DB lock.** Their writes serialise through SQLite. A heartbeat that cannot get the lock for longer than `INSTANCE_LEASE_SECONDS` sets `HEARTBEAT_STALE` (`:786-791`), which DD-9 condition 4 already refuses. <br>8. **Pre-call re-check and the residual window.** The engine runs `clock_gap_check` again immediately before the broker call, outside any transaction. If it finds a gap, the call is **not made**, a CRITICAL log line is written, and the op stays `sending`. Recovery then moves it to `unknown` (row 10). This is fail-closed: a create then follows the settle path and needs `user_recreate`. A `sending → not_sent` "provably not sent" transition is **not selected**: it would need `sent_at` cleared and a new CHECK branch, and can be revisited. The remaining window, from the pre-call check until the request leaves the process, is a **disclosed residual**. It is indistinguishable from a request delayed in transit, which M-3/M-4 already handle [I]. <br><br>**Added tests:** <br>• CAS-first detection, then the heartbeat → one `resume_seq` and exactly one `note_resume` (fails against today's `:808-811`); <br>• heartbeat-first, then a CAS → the CAS is refused by the flag although it sees no gap; <br>• a second gap during Stage 1 → that Stage 1 does not clear it; <br>• a CAS never takes `CLASSIFICATION_LOCK` and never opens a second connection (static and runtime spy); <br>• pre-call gap → no broker call, op `sending` → `unknown`; <br>• a takeover during sleep → `FencingError` before any clock logic. |

### M-7: Emergency Exit coordinated with outstanding create/modify/cancel operations

| Field | Content |
|---|---|
| **Problem** | Emergency Exit (EE) must never be blocked by a protection op (P§7.3), yet it races ops that may land. The cases: <br>• a create in flight, whose GTT may appear after EE sells; <br>• a modify `up`, whose legs may exceed the position; <br>• a cancel in flight on the GTT EE wants to cancel; <br>• `needs_review` (ownership unknown). <br>The per-position cancel index (D§3 Q3) also forces EE to cancel several known ids one after another. |
| **Options** | (a) EE waits for all ops to resolve. (b) EE ignores ops. (c) **D§3 Q1/Q3 (supersede, never take ownership), plus the refinements below.** |
| **Selected** [S] | **(c)**, engine API in S7; `routes/kill_switch` wiring in Phase 6. Per instrument: <br>1. **Refusal checks first** (today's E3 snapshot rules, `kill_switch.py:89-357` [V via D§2.7]). A refused instrument is not superseded and nothing is claimed (Q1 c). <br>2. **One fenced transaction:** claim the exit owner (existing `idx_trade_tickets_one_exit_owner`). Set the overlay (`emergency_exit`, ref = exit ticket id) on every open **create/modify** op of the position. Move each superseded `intent` to `not_sent` (row 4; the `intent→sending` CAS predicate guarantees no later send). **Open cancels are not superseded**; they continue. <br>3. **Cancel claims (`exit_prepare`)** for every known GTT id: the linked `kite_gtt_id`, each `acknowledged` `result_gtt_id`, and each open modify's `target_gtt_id`. The cancel index is unique on **`(position_id, target_gtt_id)`** (amends D§3 Q3), so the cancels are claimed together. Each sends once and is read back (time-critical). <br>4. **A target that already has an open cancel** → no second op. EE waits at most `PROTECTION_INFLIGHT_GRACE_SECONDS` for it; if it is not `confirmed` by then, the target is treated as "not confirmed gone". **[R3-3] When the wait starts and what each outcome allows are fixed by the rev. 3 R3-3 row.** <br>5. **Sell decision (D§3 Q1.6):** sell the confirmed open quantity if every known GTT is confirmed gone. ~~Otherwise sell only if attribution is `exclusive` and the broker quantity equals the app open quantity (relies on B-04).~~ Otherwise refuse the instrument and instruct the user in Kite. **[R2-3] Replaced by the G_max rule in the rev. 2 row below.** <br>6. ~~**Exclusive-only plus**~~ **`POSSIBLE_DOUBLE_SELL` watch** whenever an op on the position is `unknown` (create), `needs_review`, or a modify `up` is `sending`/`unknown`: a GTT may exist that the app cannot cancel by id, or with legs above the position. **[R2-3]** Under rev. 2 such ops make G_max > 0, so no sale happens while they are open. **[R3-2]** They are members of the rev. 3 set 𝒢 (sources 3, 5, 7). The watch is armed after **every** EE sale, for residuals not visible at sale time. <br>7. **Late results** follow the §4.3 table: flat → automatic `supersede_cleanup` cancel; exit still working → one attempt; exit failed and position open → link or `needs_review`. A modify landing on a cancelled target converges to "gone" (D:379). <br>8. After EE's sale fills (flat), every still-unconfirmed `exit_prepare` cancel becomes `none`-effect ~~and is retried automatically (M-1)~~ **and follows the M-1 rev. 2 state table [R2-2]**: same-op retry only from `unknown`; a new op after `not_sent`, or after a session-class `rejected`; nothing automatic after any other rejection. Under the rev. 2 sell rule, EE sells only once every known GTT is confirmed gone, so this step applies when the position became flat by another route (the GTT fired, or the user sold in Kite). The position is `CLOSED_CLEANUP_REQUIRED` (derived, DD-7) until confirmed. |
| **Reason** | EE must act within one click without becoming a second resolver (D:356). Keying the cancel index per target removes the queueing without weakening anything, because two cancels of different GTTs never conflict [I]. |
| **State transitions** | Rows 4, 19, 20 (§4.3 replacement), and 16 for cancels once flat. No EE path adopts, links or resolves an ambiguous op. |
| **Database** | `idx_protection_ops_one_open_cancel ON (position_id, target_gtt_id)`; overlay columns; `exit_prepare` reason. |
| **Required tests** | EE against each outstanding-op type: `intent`, `sending`, `acknowledged`, `unknown` create; modify `up` in flight; cancel in flight on the same target; `needs_review`. Two known ids → two cancels claimed in one transaction. Supersede vs send race, both orders. A refused instrument → no overlay. A late ack after a flat EE → automatic cancel; after a failed EE sale → link. ~~The exclusive-only rule and~~ the watch armed **(the exclusive-only rule is superseded by the G_max tests in the rev. 2 row [R2-3])**. A cancel coexisting with an unresolved create. |
| **Blocks** | Offline engineering: **yes, until approved** (S7). Live: **yes**: the Phase 6 `kill_switch` wiring is a separate authorisation; B-04 (over-quantity sell) remains unverified. |
| **Rev. 2: why exclusive attribution alone does NOT permit the sale [R2-3]** | Exclusive attribution is a statement about **holdings ownership**: the broker holds exactly the app's open quantity, and no shares of the user's own. It rules out one hazard: a lingering GTT, or the EE order, selling the user's own shares. It says **nothing about the GTT's state**. <br>Suppose the protective GTT for Q shares has an unresolved cancel (sent with an ambiguous outcome, or still in flight), and EE sells Q at market: <br>• **GTT fires after the sale:** a CNC SELL for Q is placed against zero holdings. <br>• **GTT fires during the sale:** two SELL orders race for the same Q shares. <br>• **A modify `up` unresolved:** the GTT's legs may exceed Q. <br>In each case the outcome — whole rejection, partial execution, or a short delivery sale with auction risk — is **B-04, unverified and never tested live**. Exclusivity cannot prevent it, because the over-sell comes from the GTT surviving, not from whose shares are held. **So exclusive attribution is necessary for any relaxation, never sufficient.** Rev. 1 step 5 effectively assumed a broker guarantee (B-04) this document must not invent. <br>(Context, not re-verified in depth: for a normal exit, the legacy code deletes the protective GTT after closing the position. The comment at `reconciliation.py:78-82` [V for the comment] says so. Rev. 2 is stricter than that legacy ordering, and Phase 6 wiring changes the live behaviour, which is already a live blocker.) |
| **Selected (rev. 2) [R2-3]: steps 4–5 replaced** | **Step 4 (rev. 2).** A target that already has an open cancel (EE's own, a cleanup's or a user's) → no second op (cancel index). ~~EE waits at most `PROTECTION_INFLIGHT_GRACE_SECONDS` for every claimed or pre-existing cancel to reach a definite result. It then takes **one** fresh complete read (purpose `emergency_exit`) that started after the last cancel was sent. A target is **confirmed gone** only by a `confirmed` cancel op (row 15), or by an explicit `deleted`/`cancelled` status in that read. Missing, `triggered`, an unknown status, or no usable read means **not confirmed gone** (B-28, B-32).~~ **[R3-3] The wait, the read and the confirmation conditions above are superseded by the rev. 3 R3-3 row, which keeps their fail-closed meaning and fixes when the wait starts and every outcome after it.** <br>**Step 5 (rev. 2): sale gate.** ~~Compute **G_max**, the most shares any GTT could still sell for this position, summed over everything not confirmed gone:~~ <br>~~• each known id (linked, `acknowledged` `result_gtt_id`, open modify `target_gtt_id`): its largest quantity across the last read and any open modify of it; for a modify `up` in `sending`/`unknown`/`needs_review`, max(old legs, requested);~~ <br>~~• each create in `sending`/`unknown`/`needs_review` (id unknowable): its `requested_quantity`;~~ <br>~~• each app-linked `triggered` GTT whose order is open or not yet seen: that order's unfilled quantity.~~ **[R3-2] The rev. 2 G_max definition above is superseded by the rev. 3 R3-2 row: a complete nine-source membership set, deduplication by broker GTT id, G_max as a count and S_agg as the aggregate quantity.** A SELL the app did not place is already refused by today's E3 rule (`routes/kill_switch.py:207-211` [V]). <br>**Sell the app open quantity only if G_max = 0** **[R3-2] and S_agg = 0, both established (rev. 3)**, attribution is `exclusive` or `shared` (D§6.3 table, unchanged), and the E3 snapshot passes (unchanged). Otherwise sell **nothing** for the instrument: result `refused`, listing each blocking GTT id or op, with the instruction "delete GTT #… in Kite, or wait for the cancel to confirm, then run Emergency Exit again". <br>**Partial sale not selected.** Selling `open − G_max` (**[R3-2]** read: open − S_agg) would need a partial exit, but the position model is full-close-only: "a user-chosen partial sell of a tracked position is not modelled" (`routes/tickets.py:352-357` [V]), and the EE ticket is sized to the open quantity (`routes/kill_switch.py:561` [V]). <br>**What is not blocked:** the owner claim, the supersede and the cancels (steps 1–3) proceed whatever the op states. Only the *sale* waits, at most the grace period (**[R3-3]** counted from the last relevant cancel sent in the click), and only on a broker fact: a GTT that may still sell the same shares. This narrows the P§7.3 "never blocked" wording to the journal (A-14). <br>**B-04-dependent relaxation (deferred).** "Sell under exclusive attribution while a cancel is unresolved" may be reconsidered only if Phase 9 evidence shows Kite deterministically rejects, in full, a triggered CNC GTT order exceeding holdings, with no partial or short execution. It would then be a new decision. <br>~~**Not selected, open to the user:** an explicit per-click user override ("sell anyway, I accept the double-sell risk"). It is a product decision, listed in §7.3.~~ **[R3-3] Closed: there is no override.** The R3 brief instructs that no "sell anyway" override be created. The question is no longer open; reopening it would be a new decision. |
| **Rev. 2 added tests [R2-3]** | Sole GTT with an `unknown` cancel and exclusive attribution → **no sale**, `refused` with the GTT id (mutation: restore the rev. 1 exclusive rule; the test must fail). The cancel confirms within the grace → sale. An explicit `deleted` in the read → sale. The target is missing from the read → no sale. An `unknown` create → no sale. A modify `up` `unknown` → G_max uses the larger quantity. An app-linked `triggered` GTT with an open order → no sale. The watch is armed after every sale. **[R3-2, R3-3]** "The cancel confirms within the grace → sale" and "explicit `deleted` → sale" hold only when every other member of 𝒢 is also removed (G_max = 0 and S_agg = 0); "a modify `up` `unknown` → G_max uses the larger quantity" now reads "S_agg uses the larger quantity" **([R4] the larger of the old and requested configurations, each summed over its SELL legs; never the sum of the two configurations)**. The rev. 3 tests are in the R3-2 and R3-3 rows; the R4 tests are in the R4 record. |
| **Rev. 3 [R3-2]: complete membership, deduplication, count and aggregate quantity** | **Scope: the instrument.** Kite holds an instrument's shares as one pool; it does not separate them per app position. So any SELL GTT on the instrument, or any order it triggered, can sell shares belonging to the position [I]. E3 already reasons per instrument for the same reason ("shares are fungible", `routes/kill_switch.py:200-216` [V]). EE still produces one result per position (`:91-140` [V]); the set below is shared by every position of the instrument. <br><br>**Membership set 𝒢** [S]: every candidate below that the R3-3 confirmation conditions have **not** removed. <br>1. **Linked protective GTTs:** the `kite_gtt_id` of every open app position on the instrument. <br>2. **Every acknowledged GTT create result:** the `result_gtt_id` of every op on those positions, in **any** state (`acknowledged`, `confirmed`, `adopted`, superseded or not). Also any id returned by a late response and recorded in `evidence` (M-4 rows 26/27), and the `linked_gtt_id` of any resolution. <br>3. **Open modify targets:** `target_gtt_id` of each modify in an open state. <br>4. **Open cancel targets:** `target_gtt_id` of each cancel in an open state, whoever claimed it (EE's own `exit_prepare`, a cleanup, a user). A cancel being open is evidence the GTT may still be active. <br>5. **Ids of unresolved or superseded ops:** target and result ids of every op in `unknown` or `needs_review`, and of every op carrying the supersede overlay. <br>6. **Broker-observed, not linked locally:** every SELL GTT on the instrument in R_EE (R3-3), or in the latest complete reads, whose status is not explicitly `deleted`/`cancelled` and which no app position links. Ownership is unknown, so it counts. <br>7. **Unknown create outcomes:** every create in `sending` (any epoch), `unknown` or `needs_review` without a `result_gtt_id`. Its GTT id is unknowable. <br>8. **Triggered GTTs with outstanding orders:** any GTT (from any source above, or listed on the instrument) in status `triggered` whose order is open, not yet visible (B-13), or in an unknown state. **[R5-2] "Whose order" means any of its triggered SELL orders. A triggered GTT also stays a member while any of its SELL legs is not proven unable to execute. It leaves only under condition (e) ~~rev. 5~~ rev. 6 [R6].** **[R6] Membership is decided on T(m), the complete triggered-order set, not on O(m): a triggered GTT stays a member, even with no outstanding order visible, until condition (e) rev. 6 establishes T(m) complete and every order in it terminal with zero remaining quantity (Rev. 6 R6 row).** <br>9. **Any other evidence:** any GTT id in local records that the application has evidence could still be active: findings such as `LATE_GTT_APPEARED` or `LINKED_GTT_MISSING`, and `gtt_observations` entries. A GTT is excluded only by a confirmation condition, never because no rule mentions it. <br>**Not members, with the reason:** an op in `not_sent` (nothing left the process). A create that ended `rejected` or `absent_after_settle`: if its GTT exists and is listed, source 6 catches it; one the listing does not show is the disclosed B-08 residual (LIVE-BLOCKER, unchanged). <br><br>**Deduplication** [S]: <br>• **Identity = broker GTT id**, normalised to its canonical string per (account, mode). Paper ids are text under INTEGER affinity (§4.1 notes). <br>• All sources that name the same id merge into **one** member, however many ops reference it. The member keeps every source as evidence~~, and its worst-case quantity is the maximum over that evidence (below)~~. **[R4] Superseded by R4: its worst-case quantity w is defined in the rev. 4 R4 row (the maximum across alternative versions of the GTT, each summed over its SELL legs, plus the sum of its outstanding triggered SELL orders).** <br>• An entry **without** a reliable id (source 7) is keyed `op:<op_id>`. It is never merged with another id-less entry, nor with an id entry, until the op records a `result_gtt_id` (by `acknowledged`, adoption or a late response). Then it becomes that id's source. **[R5-1] While it is id-less, its w is UNKNOWN, so S_agg is UNKNOWN (B-24: the number or identity of resulting GTTs is not established). Recording one id never proves that the request created only one GTT. Any sibling is a separate member through source 6 or the ambiguous-id rule (Rev. 5 R5-1 row).** <br>• **Two different ids are never merged**, whatever their fingerprint, shape or quantity. They are two GTTs, never "harmless duplicates" (B-24, B-29). <br>• **Ambiguous id** (an op with conflicting ids, more than one fingerprint candidate, a malformed id in a response, an id linked to two positions): every candidate becomes a separate member. The instrument's attribution is treated as ambiguous for this evaluation, an URGENT finding is raised, and the gate fails closed. <br>• The rule may over-count (an id-less create may be the same GTT as an unlinked id); it never under-counts. Over-counting cannot cause a wrong sale, because the gate is zero against non-zero. <br><br>**Count and aggregate quantity** [S]: <br>• **G_max = \|𝒢\|**: the number of distinct potentially active sell sources after deduplication. <br>• **S_agg = Σ w(m)** over the members, the aggregate worst-case sell quantity, where: <br>&nbsp;&nbsp;– ~~a known id: w = the maximum of (the sum of its SELL leg quantities in the latest complete read listing it; the old and requested quantities of every unresolved modify of it; its requested quantity at creation). Two-leg mutual exclusion is not relied on [U];~~ <br>&nbsp;&nbsp;– ~~an id-less create: w = its `requested_quantity`, a **lower bound**, because one request may have produced more than one GTT (B-24);~~ <br>&nbsp;&nbsp;– ~~a triggered member: w = its order's unfilled quantity, or its leg quantity if the order is not yet visible;~~ <br>&nbsp;&nbsp;**[R4] The three `w` bullets above are superseded by R4: w(m) is defined in the rev. 4 R4 row below.** <br>&nbsp;&nbsp;– any member whose quantity cannot be read: S_agg = **UNKNOWN** (kept; extended by R4 to every ambiguity of identity, version, leg quantity, order status or completeness; **[R5-1]** and by R5 to an id-less create whose resulting GTTs are not all identified). <br>• S_agg is **never capped** at the broker holding or at the app open quantity. Two SELLs (a GTT order and the EE order, or two GTT orders) are **not** assumed unable to execute against the same shares (B-04). <br><br>**Sale gate** [S]: EE submits the SELL for a position only if **all** hold: 𝒢 was established for the instrument; **G_max = 0**; **S_agg = 0**; attribution is `exclusive` or `shared` (D§6.3, unchanged); the E3 snapshot passes (unchanged). The set cannot be established when: there is no usable R_EE (R3-3); the read is partial or stale; the journal or evidence cannot be read; or an id is ambiguous. Then nothing is sold for the instrument: the result is `refused` and lists each member (GTT id or op id), its source and its w, with the instruction "delete GTT #… in Kite, or wait for the cancel to confirm, then run Emergency Exit again". G_max = 0 implies S_agg = 0 by construction; both are required so that a defect in either calculation is caught. <br><br>**Step 3 is unchanged.** EE claims `exit_prepare` cancels only for the ids listed in step 3. Members that step 3 does not cancel (source 6 unlinked GTTs, source 7 id-less creates, other non-app-proven ids) are **not** cancelled by EE. They block the sale and are listed. EE never cancels a GTT it cannot prove is the app's (the M-2 condition 1 rationale). <br>**Added tests:** see [R3-2] in the revision history. |
| **Rev. 4 [R4]: S_agg as the multi-leg worst-case sell quantity** | **Why.** The R3-2 `w` bullets could under-count. An id-less create used `requested_quantity`, a single integer, even when its `request_payload` carries two SELL legs. An unresolved modify gave "old and requested quantities" without saying how legs combine within each configuration. A triggered member assumed one order. "The maximum over that evidence" would take the maximum, not the sum, of independent legs and orders. <br><br>**Definition** [S]. **S_agg** is the aggregate worst-case quantity of shares that could still be sold, across the **entire instrument**, by all distinct potentially active GTTs in 𝒢 and their outstanding triggered SELL orders: **S_agg = Σ w(m)** over the members m of 𝒢, deduplicated as in R3-2. <br><br>**Terms.** <br>• A **version** of a GTT is one complete configuration it may currently have at the broker: its legs, each with transaction type, quantity, trigger and limit. <br>• A **potentially executable SELL leg** is any SELL leg of a version that authoritative evidence (the R3-3 conditions) has not established as unable to execute. Two legs are **never** assumed mutually exclusive: two-leg OCO behaviour is [U] and is not relied on. **[R5-2] A terminal order of one leg establishes nothing about another leg (condition (e) rev. 5).** <br>• **L(v)** = the sum of the quantities of the potentially executable SELL legs of version v. <br>• **V(m)** = the set of possible versions of member m (by case, below). <br>• **O(m)** = the set of outstanding triggered SELL orders of m; **r(o)** = the remaining unfilled SELL quantity of order o. **[R6] O(m) is the outstanding subset of T(m), the complete triggered-order set of m (every triggered SELL order associated with m, whether open, partially filled, terminal, cancelled or rejected, plus every expected order not yet visible). Only O(m) enters w(m); T(m) is used by condition (e) rev. 6 (Rev. 6 R6 row).** <br><br>**Rule** [S]: **w(m) = max { L(v) : v ∈ V(m) } + Σ { r(o) : o ∈ O(m) }**. <br>• Within a version, every potentially executable SELL leg is **summed**. <br>• Across the versions of the same GTT, the **maximum** is taken, never the sum: they are alternative states of one GTT, of which only one is live. <br>• Outstanding triggered orders are **summed** with each other and added to the version term. A leg whose triggered order is visible counts through that order's r(o), not again in L(v). A leg that triggered without a visible order (B-13) counts at its full leg quantity. If which leg triggered cannot be established, every SELL leg counts. Over-counting is safe (R3-2: the gate is zero against non-zero); under-counting is not allowed. <br><br>**V(m) and O(m) by case** [S]: <br>1. **Broker-listed GTT** (any source whose id a complete read lists): V includes the version in the latest complete read listing the id (R_EE when it lists it). **Every** SELL leg of that version counts, and the quantities of all potentially executable SELL legs are summed. <br>2. **Unresolved CREATE** (a create in `sending`, `unknown`, `needs_review`, or `acknowledged` before its read-back): V includes the version in the op's `request_payload`. **Every** SELL leg in the payload counts; `requested_quantity` alone is never used when the payload has more than one SELL leg. ~~An id-less create (source 7) keeps this as a **lower bound**, because one request may have produced more than one GTT (B-24).~~ **[R5-1] Superseded by R5: an id-less create (source 7) whose resulting GTTs are not all identified has w UNKNOWN, so S_agg is UNKNOWN. Its payload quantity is never presented as a worst case or a bound (Rev. 5 R5-1 row).** When the create has an id, the payload version is added to that id member's V. **Fail closed** (w UNKNOWN) when the payload is missing or not valid JSON, has no leg list, has a leg without a transaction type or quantity, has a leg count inconsistent with its trigger type (`single` → one, `two-leg` → two), or names another instrument. For a known id whose creating op is resolved, that op's recorded payload stays a version when the journal holds it (carried over from R3-2's "requested quantity at creation"). <br>3. **Unresolved MODIFY** (a modify on m in `sending`, `unknown`, `needs_review`, or `acknowledged` before its read-back): V includes **both** the **old** version and the **requested** version. The old version is the target's configuration in the complete read the op relied on (its `broker_reads.id` in `evidence`), or else in the latest complete read that listed the target before the op's `sent_at`. The requested version is the op's `request_payload`. Each is summed over its own SELL legs, and the **greater** aggregate is used, never their sum. With several unresolved modifies of one GTT, each requested version is added to V. If **either** version cannot be established, w is UNKNOWN and the gate refuses. <br>4. **Unresolved CANCEL** (a cancel on m in any state, as long as no R3-3 condition (a)–(e) has removed m): the cancel changes nothing. m keeps its full w until authoritative evidence, an R3-3 removal condition, establishes that it can no longer execute. A cancel acknowledgement alone never reduces w or removes m (R3-3 G4, G5). <br>5. **Triggered GTTs and outstanding orders** (source 8, or any member listed `triggered`): O(m) contains every triggered SELL order of m that could still execute, meaning a read shows it open, partially filled or in an unknown state, plus any expected order not yet visible (B-13). r(o) = its quantity minus its filled quantity, as read. Separate outstanding orders are **summed** whenever each could still execute; two orders, or a GTT leg and an order, are never assumed unable to both execute (B-04). The same broker order id reached through several sources counts once. Different order ids are summed. An order whose identity, status or remaining quantity is ambiguous makes w UNKNOWN. **[R5-2]** An order that is terminal with nothing left to fill leaves O(m), but the member leaves 𝒢 only under condition (e) ~~rev. 5~~ rev. 6 [R6]. Every other SELL leg that is not proven unable to execute stays in L(v). **[R6]** Such an order leaves O(m) but stays in T(m). An expected order not yet visible is in both T(m) and O(m), and is counted once, as above (B-13). The member leaves 𝒢 only under condition (e) rev. 6, which evaluates T(m). <br><br>**Deduplication and aggregation** [S] (the R3-2 rules kept, applied to w): <br>• one member per broker GTT id; id-less entries are never merged; different ids are never merged; an ambiguous id fails closed; <br>• the alternative versions of one member are combined by maximum, so one GTT is never double-counted through its versions or its several sources; distinct members are always summed; <br>• S_agg is **never capped** at the holding or at the open position quantity; <br>• if any member's identity, any required version, any leg quantity, any order's status or remaining quantity, or the completeness of 𝒢 cannot be established, **S_agg = UNKNOWN** and the gate refuses the sale. <br><br>**Sale gate unchanged.** EE may sell only when the complete instrument-wide set 𝒢 is established, **G_max = 0** and **S_agg = 0** (R3-2 "Sale gate"; R3-3 table). There is no override, no automatic later sale, and no weaker reading. R4 changes how S_agg is computed and reported (each listed member shows its w) and how S_agg cross-checks G_max. It does not change when EE sells. <br>**Unchanged:** the nine membership sources, step 3 (which ids EE cancels), the grace rules and the grace-outcome table, and the partial-sale refusal. <br>**Added tests:** T1–T3 and the fail-closed cases in the [R4] record of the revision history. |
| **Rev. 5 [R5-1]: id-less CREATE multiplicity (B-24)** | **Why.** R4 case 2 kept an id-less create's payload quantity "as a lower bound" (B-24). S_agg is the **worst-case** aggregate, and a lower bound is not a worst case. One request may have created more than one GTT. <br><br>**Three different facts** [S]: <br>(i) **A known request payload quantity:** the sum of the SELL legs in the op's `request_payload`. It is what **one** GTT made from that request could sell (one version, R4 case 2). <br>(ii) **A proven complete set of resulting GTTs:** every GTT the request produced is identified by its broker GTT id, so each is a member of 𝒢 under its own id. <br>(iii) **An unknown number or identity of resulting GTTs.** <br>(i) never establishes (ii) and never rules out (iii). <br><br>**Rule** [S]: <br>1. **Unknown multiplicity → UNKNOWN.** An id-less create (source 7, key `op:<op_id>`) whose resulting GTTs' number or identity cannot be established has **w = UNKNOWN, so S_agg = UNKNOWN**. Under B-24 this covers every create with no recorded `result_gtt_id`. <br>&nbsp;&nbsp;– The payload quantity is never presented as the member's worst-case quantity, nor as a lower or upper bound. <br>&nbsp;&nbsp;– The refusal lists the op with w UNKNOWN. It may show the payload figure only when it is labelled "per-GTT payload quantity; number of resulting GTTs unknown". <br>2. **Identified resulting GTTs → separate members.** A resulting GTT is identified by a `result_gtt_id` recorded by acknowledgement, adoption, a late response or a manual resolution (R3-2), or by its own id in a complete read. Each identified GTT is a separate member: <br>&nbsp;&nbsp;– it is deduplicated by broker GTT id (R3-2); <br>&nbsp;&nbsp;– its w is computed individually under R4, and while the create is unresolved, the payload version joins that id's V(m); <br>&nbsp;&nbsp;– two GTTs identified from one request are two members, summed in S_agg. <br>3. **One id is not one GTT.** Recording one `result_gtt_id` never proves that the request created only one GTT. A further GTT the request may have created is never inferred away: <br>&nbsp;&nbsp;– it is a separate member through source 6 whenever a complete read lists it; <br>&nbsp;&nbsp;– more than one fingerprint candidate sends the op to `needs_review` (row 18), and every candidate becomes a separate member under the R3-2 ambiguous-id rule; <br>&nbsp;&nbsp;– a resulting GTT that no complete read lists is the disclosed B-08 residual (LIVE-BLOCKER, unchanged). <br>4. **No merging.** Id-less entries are never merged with each other or with an id member (R3-2, unchanged). One request is never assumed to have created only one GTT. <br>5. **Fail closed.** Any ambiguity in multiplicity, identity, payload completeness (the R4 case 2 fail-closed list) or resulting GTT state makes S_agg UNKNOWN, and the gate refuses. <br>6. **Gate unchanged.** An unresolved id-less create is a member (source 7, grace row G21), so G_max > 0 and S_agg is UNKNOWN. Emergency Exit refuses the sale; there is no override and no deferred sale. The `op:<op_id>` entry leaves 𝒢 only under R3-3 condition (d). <br><br>**Unchanged:** B-24 and its Blocks classification (LIVE-P9, disclosed residual); the R3-2 membership and deduplication; the R4 formula and its payload rule for creates with an id. <br>**Added tests:** T4 and its positive control in the [R5-1] record of the revision history. |
| **Rev. 3 [R3-3]: grace-period semantics and the authoritative outcome table** | **Relevant cancels.** EE's own `exit_prepare` cancels claimed in step 3, plus every pre-existing open cancel whose target is a member of 𝒢. <br><br>**A. When the grace starts.** At **T_last**, the latest `sent_at` (committed with `sending`) among the relevant cancels **sent during this EE click**. <br>**B. Initiation or last cancel?** **The last cancel sent**, not EE initiation. Cancels go out one after another, so a window started at initiation would shorten, or eliminate, the in-flight window of a late cancel. Pre-existing cancels sent before the click add no new wait: their own window has already run, or runs from their own `sent_at`. **If no cancel was sent in this click**, there is no wait. <br>**End.** T_g = T_last + `PROTECTION_INFLIGHT_GRACE_SECONDS`, measured on the in-process monotonic clock (wall-clock `sent_at` is recorded). EE stops waiting earlier once every relevant cancel has a definite op result (`confirmed`, `rejected`, or `acknowledged` with its read-back done). The longest wait is the time taken to send the click's cancels plus the grace. <br>**The read.** Then exactly **one** complete read R_EE (purpose `emergency_exit`) with `started_at ≥ max(EE initiation, T_last)`, satisfying DD-8 rules i–iii (including post-resume and post-takeover). R_EE also supplies sources 6 and 8 of 𝒢. <br><br>**The only conditions that remove a member from 𝒢:** <br>(a) R_EE lists the id with an explicit `deleted` or `cancelled` status; <br>(b) a cancel op on the id is `confirmed` (rows 11/15, which themselves require an explicit status), **and** R_EE does not list the id in any other status; <br>(c) a manual resolution `deleted_in_kite_verified` for the id (Q3; explicit status in its evidence read), **and** R_EE does not list the id in any other status; <br>(d) an id-less create (source 7): its op left the open set with no GTT (`rejected`, `not_sent`, `absent_after_settle`, or resolved `no_app_gtt`/`deleted_in_kite_verified`). If it gained an id, it becomes an id member instead; <br>~~(e) a triggered member: R_EE shows its order terminal with zero pending quantity, and the resulting fill is recorded on the position.~~ **[R5-2] Superseded by R5:** <br>~~(e) **a triggered member**, only when complete, unambiguous evidence (R_EE, the order reads and the position's recorded fills) establishes **all** of the following:~~ <br>&nbsp;&nbsp;~~1. every outstanding triggered SELL order associated with the GTT is terminal;~~ <br>&nbsp;&nbsp;~~2. every such order has zero remaining unfilled quantity: the read shows no quantity of the order that can still fill, and any quantity that did not fill is reported by the broker as cancelled or rejected, never inferred;~~ <br>&nbsp;&nbsp;~~3. every other SELL leg of the GTT is proven unable to execute. Proof is either that leg's own triggered order satisfying items 1–2, or an explicit `deleted`/`cancelled` status of the GTT in R_EE (items 1, 2 and 4 still apply to every order already triggered). The `triggered` status, an assumed OCO behaviour (two-leg exclusivity [U]) and the absence of another order are never proof;~~ <br>&nbsp;&nbsp;~~4. all resulting fills are recorded and reconciled against the relevant position;~~ <br>&nbsp;&nbsp;~~5. the evidence covers the complete GTT: every version in V(m) (R4), every SELL leg and every triggered order. An expected order that is not yet visible (B-13) makes the evidence incomplete.~~ <br>~~If any item cannot be established: the member stays in 𝒢; its known remaining exposure stays in S_agg (R4: r(o) of each outstanding order plus every potentially executable leg in L(v)); if that exposure or the completeness cannot be established, S_agg = UNKNOWN; Emergency Exit refuses. A single terminal order is never proof that a multi-leg GTT is harmless (Rev. 5 R5-2 row).~~ <br>**[R6] The rev. 5 condition (e) above is superseded by R6.** Its item 1 asked "outstanding" orders to be terminal, which contradicts the R4 meaning of O(m), and it did not name the complete set of triggered orders. <br>(e) **rev. 6, a triggered member**, only when complete, unambiguous evidence (R_EE, the order reads, the evidence on the GTT's trigger and legs, and the position's recorded fills) establishes **all** of the following about **T(m)**, the complete triggered-order set (R4 row, Terms; Rev. 6 R6 row), and not merely about the current members of O(m): <br>&nbsp;&nbsp;1. **T(m) is complete.** Every triggered SELL order associated with the GTT is identified: open, partially filled, terminal, cancelled or rejected, and every order whose existence the evidence on the GTT's trigger and legs indicates. An expected order that is not yet visible in the broker listing (B-13) is a member of T(m) and makes the evidence incomplete; it is never omitted. If the evidence cannot establish which orders should exist, T(m) is not complete; <br>&nbsp;&nbsp;2. every order in T(m) is proven terminal; <br>&nbsp;&nbsp;3. every order in T(m) has zero remaining unfilled quantity: the read shows no quantity of the order that can still fill, and any quantity that did not fill is explicitly reported by the broker as cancelled or rejected, never inferred from missing data; <br>&nbsp;&nbsp;4. every SELL leg of the GTT is proven unable to execute. Proof is either that the leg's triggered orders, all in T(m), satisfy items 2–3, or an explicit `deleted`/`cancelled` status of the GTT in R_EE (items 1–3, 5 and 6 still apply to every order already triggered). The `triggered` status, an assumed OCO behaviour (two-leg exclusivity [U]) and the absence of another order are never proof; <br>&nbsp;&nbsp;5. all resulting fills are recorded and reconciled against the relevant position; <br>&nbsp;&nbsp;6. the evidence covers the entire GTT: every version in V(m) (R4), every SELL leg and every order in T(m). <br>Items 1–3 imply that O(m) is empty; an empty O(m) does not imply items 1–3. If any item cannot be established: the member stays in 𝒢; all its known outstanding exposure stays in S_agg under the unchanged R4 calculation (r(o) of each order in O(m), plus every potentially executable leg in L(v)); if that exposure or the completeness of T(m) cannot be established, S_agg = UNKNOWN; Emergency Exit refuses. A terminal order is never an outstanding order (it is in T(m), not in O(m), and contributes 0). An empty current outstanding-order list is never proof that T(m) is complete or harmless. A single terminal order is never proof that a multi-leg GTT is harmless (Rev. 5 R5-2 row; Rev. 6 R6 row). <br>**Never sufficient on its own:** a cancel acknowledgement; a cancel in `sending`, `unknown`, `rejected`, `not_sent` or `needs_review`; the id missing from R_EE (B-28); `triggered` with the order open or unseen; an unknown status (B-32); a stale, partial or failed read; **[R5-2]** one triggered order terminal with zero remaining quantity while another SELL leg or triggered order of the same GTT can still execute or is not established; **[R6]** an empty O(m), or one or more visible terminal orders, while T(m) is not established as complete (for example an expected triggered order not yet visible, B-13). <br><br>**C. A cancel still UNKNOWN after the grace** does **not** permit the SELL. The member stays in 𝒢 unless R_EE shows an explicit `deleted`/`cancelled` (condition a). <br>**D. A fresh complete read lists the GTT ACTIVE** after its cancel was accepted: the SELL is **not** permitted, whatever the op says. If the op is already `confirmed`, the evidence contradicts it: URGENT finding, position `PROTECTION_NEEDS_REVIEW`. The terminal op is not changed (§4.2 "never allowed"). <br><br>**Authoritative outcome table** (per member, evaluated once the wait ends; "refuse" = no SELL for the instrument, result `refused`, the member listed): <br>see "M-7 rev. 3 grace-outcome table [R3-3]" directly below this table. <br><br>**After a refusal.** No SELL is submitted later in the background ("sell when clear" is not selected), and there is **no "sell anyway" override**. Each op continues on its own path: reads, the R2-2 state table, M-1 (an `exit_prepare` is not retried automatically while the position is open) and M-3. The user may click Emergency Exit again. The cancel index prevents a duplicate cancel, so no cancel is sent and no new grace starts; a new R_EE is read and 𝒢 is re-evaluated. <br>**Added tests:** see [R3-3] in the revision history. |
| **Rev. 5 [R5-2]: condition (e) for triggered multi-leg GTTs** | **Why.** The rev. 3 condition (e) removed a triggered member when "its order" was terminal with zero pending quantity. Under the R4 multi-leg model, a GTT may have several SELL legs and several triggered SELL orders. One terminal order does not prove that the others cannot execute. <br><br>**Rule** [S]: condition (e) as restated in the R3-3 row above (items 1–5, all required, from complete and unambiguous evidence). **[R6] Restated again by R6 as items 1–6 over the complete triggered-order set T(m) (Rev. 6 R6 row); the consequences below are unchanged.** Consequences: <br>• **Partial resolution keeps the member.** If one leg's order is terminal with nothing left to fill but another SELL leg, or its triggered order, can still execute, the member stays in 𝒢 (G6). The finished order leaves O(m), contributing 0 **([R6] it stays in T(m))**. The other leg counts in L(v) at its leg quantity, or through r(o) of its order (R4). S_agg includes that remaining exposure, and Emergency Exit refuses. <br>• **Unknown remainder → UNKNOWN.** If the status, quantity or completeness of any other leg or order cannot be established, S_agg = UNKNOWN, and Emergency Exit refuses. <br>• **Nothing is inferred from the GTT status.** `triggered` says a leg fired. It does not say the other legs are dead (two-leg OCO exclusivity [U], not relied on). <br>• **Reconciliation is required.** A fill that is not recorded and reconciled against the relevant position keeps the member. <br><br>**Unchanged:** conditions (a)–(d); C and D; the grace start at T_last and the one fresh complete R_EE; the cancellation acknowledgement rules (G4, G5); the R2-2 table; the G1–G22 numbering and order; no override and no deferred sale. G6 is reworded (table below) so that it cannot remove a multi-leg GTT on one terminal order. <br>**Added tests:** T5, its fail-closed variant and its control in the [R5-2] record of the revision history. |
| **Rev. 6 [R6]: complete triggered-order set T(m) and outstanding subset O(m)** | **Why.** R4 defined O(m) as the orders that could still execute. Rev. 5 condition (e) item 1 then asked every "outstanding" order to be terminal. Under the R4 meaning that is a contradiction. Read loosely, it checks only the orders currently outstanding, so an empty O(m) could pass for proof that a triggered GTT is harmless, even while an expected order is not yet visible. <br><br>**Definitions** [S]: <br>• **T(m), the complete triggered-order set** of member m: every triggered SELL order associated with the GTT, including orders that are currently open, partially filled, terminal, cancelled or rejected, **and** every order whose existence the evidence on the GTT's trigger and legs indicates but which is not yet visible in the broker listing (B-13). T(m) is a **historical and current completeness** concept, not a snapshot of the orders open now. An expected but not-yet-visible order is never silently omitted: its presence makes the evidence incomplete. If the evidence cannot establish which orders should exist, T(m) cannot be established as complete. <br>• **O(m), the outstanding triggered-order subset** (the R4 meaning, unchanged): **O(m) ⊆ T(m)**, the triggered SELL orders that could still execute: open, partially filled, or otherwise unresolved (unknown status, ambiguous identity, or expected but not yet visible). A terminal order proven incapable of further execution is **not** in O(m). <br><br>**Uses** [S]: <br>• **Exposure uses O(m).** w(m) = max { L(v) : v ∈ V(m) } + Σ { r(o) : o ∈ O(m) } and S_agg = Σ w(m) are unchanged (R4). A proven terminal order contributes 0 and is never counted as outstanding sell exposure. An expected but not-yet-visible order is counted once, at its leg's full quantity (R4 B-13 rule), or makes w UNKNOWN when that quantity cannot be established. <br>• **Removal uses T(m).** Condition (e) rev. 6 (R3-3 row) requires T(m) to be complete and every order in it terminal with zero remaining unfilled quantity, every SELL leg proven unable to execute, every fill reconciled, and evidence covering the entire GTT. <br>• **No inference either way.** A terminal order is never treated as outstanding. An empty current outstanding-order list is never proof that T(m) is complete or harmless. One or more visible terminal orders never prove completeness. <br><br>**Unchanged:** the R4 formula and quantity rules (including the maximum across listed, create-payload and old/requested MODIFY versions); R5-1 and B-24; the R5-2 safeguards; conditions (a)–(d), C and D; the grace start at T_last, the cancellation rules and the one fresh complete R_EE; the G1–G22 numbering and every refusal outcome; the sale gate; no override and no deferred sale. <br>**Added tests:** T6 in the [R6] record of the revision history. |

**M-7 rev. 3 grace-outcome table [R3-3]** (the single authority for every grace-period outcome; it decides only the
sale and never changes an op's transitions; G22 is checked first; then rows are read in order and the first matching row applies):

| # | Cancel op state on the member when the wait ends | R_EE status of the member's id | Member removed? | EE sale outcome | Op handling (unchanged rules) |
|---|---|---|---|---|---|
| G1 | `confirmed` | explicit `deleted`/`cancelled`, or not listed | **yes** (b) | sale if every other member is removed and the other gate conditions hold | none |
| G2 | `confirmed` | `ACTIVE`, `triggered` or an unknown status | no: contradiction | **refuse**; URGENT finding; position `PROTECTION_NEEDS_REVIEW` | terminal op unchanged |
| G3 | `acknowledged` (accepted, read-back pending) | explicit `deleted`/`cancelled` | **yes** (a) | as G1 | row 11 → `confirmed` |
| G4 | `acknowledged` | `ACTIVE` | no: an acknowledgement is not proof | **refuse** | read-back continues (rows 11–13) |
| G5 | `acknowledged` | not listed | no (B-28) | **refuse** | as G4 |
| G6 | any state **[R5-2] (or no cancel)** | `triggered` | ~~no: source 8, w = unfilled order quantity (**[R4]** the remaining quantities of all its outstanding triggered SELL orders, summed, plus any SELL leg still potentially executable; M-7 rev. 4)~~ ~~**[R5-2] Superseded by R5:** **no** (source 8), **unless** condition (e) rev. 5 holds in full for the whole GTT: every triggered SELL order terminal with zero remaining unfilled quantity, every other SELL leg proven unable to execute, every fill recorded and reconciled, and evidence covering the complete GTT → **yes** (e). One terminal order with zero remaining quantity **never** removes a multi-leg GTT while another SELL leg or triggered order of it can still execute or is not established. While the member is retained, w = the remaining quantities of all its outstanding triggered SELL orders, summed, plus every SELL leg still potentially executable (M-7 rev. 4); UNKNOWN if that cannot be established~~ **[R6] Superseded by R6:** **no** (source 8), **unless** condition (e) rev. 6 holds in full for the whole GTT → **yes** (e). Condition (e) evaluates the **complete** triggered-order set **T(m)**: T(m) established as complete; every order in T(m) terminal with zero remaining unfilled quantity (any unfilled quantity explicitly reported by the broker as cancelled or rejected); every SELL leg proven unable to execute; every fill recorded and reconciled; evidence covering the entire GTT, all its SELL legs and every order in T(m). An expected triggered order that is not yet visible (B-13) makes the evidence incomplete → **no**. One or more visible terminal orders do **not** prove that T(m) is complete, and an empty O(m) is never proof → **no**. One terminal order with zero remaining quantity **never** removes a multi-leg GTT while another SELL leg or triggered order of it can still execute or is not established. While the member is retained, w uses **O(m)**, the outstanding subset, under the unchanged R4 calculation: the remaining quantities of all orders in O(m), summed, plus every SELL leg still potentially executable (M-7 rev. 4); proven terminal orders contribute 0; UNKNOWN if that exposure or the completeness of T(m) cannot be established | ~~**refuse** whenever complete (e) evidence is unavailable; as G1 only when (e) rev. 5 removes the member~~ **[R6]** **refuse** whenever the complete (e) rev. 6 evidence for T(m) is unavailable; as G1 only when (e) rev. 6 removes the member | the trigger path (D:777-780); rows 15a/15b |
| G7 | any state | an unknown status | no (B-32) | **refuse** | `needs_review` per rows 15a/15b |
| G8 | `sending` (in flight past T_g) | explicit `deleted`/`cancelled` | **yes** (a) | as G1 | rows 9/10 (`unknown` by recovery), then row 15 |
| G9 | `sending` | `ACTIVE` or not listed | no | **refuse** | as G8 |
| G10 | `unknown` | explicit `deleted`/`cancelled` | **yes** (a) | as G1 | row 15 → `confirmed` |
| G11 | `unknown` | `ACTIVE` | no (C) | **refuse** | stays `unknown`; no same-op retry by EE while the position is open (M-1, R2-2) |
| G12 | `unknown` | not listed | no (C, B-28) | **refuse** | settle, then row 15a → `needs_review` |
| G13 | `rejected` (any class) | explicit `deleted`/`cancelled` | **yes** (a) | as G1 | terminal; never resent (R2-2) |
| G14 | `rejected` | `ACTIVE` or not listed | no | **refuse**; a "does not exist" refusal with an `ACTIVE` listing also raises the B-17 URGENT finding | R2-2 table (a new op only after a session-class rejection once the effect is `none`) |
| G15 | `not_sent` (the cancel never left) | explicit `deleted`/`cancelled` | **yes** (a) | as G1 | terminal |
| G16 | `not_sent` | `ACTIVE` or not listed | no | **refuse** | R2-2 table |
| G17 | `needs_review` | explicit `deleted`/`cancelled` | **yes** (a) | as G1 | the op still needs a human resolution (Q3) |
| G18 | `needs_review` | `ACTIVE` or not listed | no | **refuse** | Q3 |
| G19 | no cancel exists (sources 5, 6, 9; not cancelled by step 3) | explicit `deleted`/`cancelled` | **yes** (a) | as G1 | none |
| G20 | no cancel exists | `ACTIVE`, not listed, `triggered` or an unknown status (**[R5-2]** a `triggered` listing is decided by G6, which precedes this row; **[R6]** G6 applies condition (e) rev. 6 to the complete set T(m)) | no | **refuse**; listed for deletion in Kite | none |
| G21 | id-less create (source 7) still open | any | no (id unknowable). **[R5-1]** w UNKNOWN, so S_agg UNKNOWN; the payload quantity is never a worst case or a bound | **refuse** | settle / adoption (rows 14, 17, 18) |
| G22 | any | **no usable R_EE** (failed, partial, stale, or started too early) | no: 𝒢 not established | **refuse** the instrument; list every candidate | none |

### M-8: the `absent_after_settle` CHECK inconsistency

| Field | Content |
|---|---|
| **Problem** | Three statements disagree: <br>• D:1145 (§11.2): CHECK `sent_at IS NOT NULL AND complete_reads_after >= 2 AND kind = 'create'`; <br>• D:319 (§3 Q2): the same **without** `kind = 'create'`; <br>• D:475 (row 21) lists `absent_after_settle` as a resolution of "any open + overlay", which would include modify and cancel. <br>The settle rule (D:328) and fingerprint matching only make sense for a create, whose GTT id is unknown. Modify and cancel target a known id. |
| **Options** | (a) Follow D:319 (any kind). (b) **Follow D:1145 (create only) and correct D:319 and row 21.** (c) Add a separate terminal for a modify/cancel target that is missing. |
| **Selected** [S] | **(b).** CHECK `state != 'absent_after_settle' OR (kind = 'create' AND sent_at IS NOT NULL AND complete_reads_after >= 2)`. Row 21 is amended to "`absent_after_settle` (create only)". Modify/cancel target-missing goes to `needs_review` (M-3), not to a new terminal (c). |
| **Reason** | For a known target, "missing" is a contradiction about a GTT the app knows, which needs a human while listing completeness is unverified. It is not the same inference as "my create never landed". A new terminal (c) would be a second inferred-absence state with weaker evidence. |
| **State transitions** | Row 17: create only. Rows 15a/15b (§4.2) for modify/cancel. |
| **Database** | The CHECK above (in §4.1). |
| **Required tests** | Insert or update to `absent_after_settle` with `kind` modify or cancel → refused. Create with `complete_reads_after = 1` → refused. Create with `sent_at NULL` → refused. Mutation: drop `kind = 'create'`. |
| **Blocks** | Offline engineering: **yes, until approved** (S1). Live: no. |

### M-9: migration v12 renumbering and compatibility

| Field | Content |
|---|---|
| **Problem** | `_m011_*`/`_r011_*` collide with the registered v11 (`db.py:2220-2222, 2256, 2838, 2855` [V]). Compatibility rules for a future v12 must be fixed before Phase 3 code depends on the names. |
| **Options** | Names: keep the `_dark` suffix, or use the registration name now. Present-state handling: mirror v11, or refuse. Rollback: the dark rollback only, or also a `rollback_schema_v12`. |
| **Selected** [S] | **Names:** `_m012_protection_journal` and `_r012_protection_journal`. Future registration entry `(12, "phase3_protection_journal", _m012_protection_journal)`, appended only (the `db.py:2243-2244` rule). The callers `apply_dark_protection_schema`/`rollback_dark_protection_schema` (`db.py:2889, 2925`) call the new names. <br>**`_m012` behaviour:** <br>• `absent` → create; <br>• `present` (exactly v12's objects) → no-op that keeps data, mirroring `_migration_v11_instance_coordination` (`db.py:2222-2227` [V]); <br>• `partial`/`unexpected` → refuse; <br>• it never calls `_run_dark_step` (it does not today, RR§7.3); <br>• registered-mode duplicate refusal per DD-6. <br>**Rollback:** `_r012` drops exactly the v12 objects, including the resolutions table and the 13-type event suffix. A future `rollback_schema_v12`, mirroring `rollback_schema_v11` (`db.py:2459-2511` [V]), refuses unless: <br>• the history is exactly v1..v12; <br>• the objects are exactly v12's; <br>• no journal, resolution, `protection_state` or `protection_*` event data exists. <br>It then also deletes `schema_version` row 12. It is written and tested in S2; it is not exposed as a CLI until registration. <br>**Dark apply precondition:** stays "the database is at `LATEST_SCHEMA_VERSION`" (11 during Phase 3). <br>**Compatibility:** <br>• v11 and v10 builds refuse a v12 database (`_check_history`, `db.py:2283-2288` [V]); <br>• `rollback_schema_v11` refuses on a v12 database (exact-history rule, `db.py:2471-2477` [V]), so the order is v12 rollback then v11 rollback; <br>• a v10 → v12 upgrade runs both migrations after **one** backup named `pre_schema_v12_bak` that holds the **v10** image (disclosed in the registration runbook); <br>• if v12 fails, the database stays at v11, which the Phase 2 build accepts. <br>**After the engine writes data, rollback means restoring a backup**, because rollback refuses while data exists. This is stated before registration. |
| **Reason** | The names describe the registered object, not a phase of darkness. The `present` handling matches v11's tested pattern (a lost `schema_version` table). The exact-history rollback keeps the version chain strict. |
| **State transitions** | None. |
| **Database** | Renames in S1 (dark code only). `rollback_schema_v12` in S2. `MIGRATIONS` unchanged (DD-2). |
| **Required tests** | Update the pins at `tests/test_dark_protection_schema.py:194` (identity set) and `:688` (regex `_m012_\|_r012_`). The RR§7.5 list 1–8 on scratch databases: <br>• chain v10→v11→v12 with registration simulated as `MIGRATIONS + [v12]` in-test; <br>• failure injection (exception, `KeyboardInterrupt`, `os._exit` child) leaves v11; <br>• `MIGRATIONS[:11]`/`[:10]` refuse v12; <br>• rollback ordering; <br>• duplicate-GTT branch; <br>• account-database refusal for the renamed functions. |
| **Blocks** | Offline engineering: **yes** (mechanical, S1). Live: **yes**: registration is a separate gate (DD-2). |

### M-10: broker-dependent assumptions that remain unverified

| Field | Content |
|---|---|
| **Problem** | The engine is fail-closed against broker unknowns, but several of its parameters and relaxations, and the sufficiency of its protection, rest on facts no offline test can establish (RR§5, §8, §9). |
| **Options** | (a) Leave them scattered across P§10, D§12.2 and U6M§8. (b) **One register, with this document's new assumptions added, each with its offline posture and resolving evidence.** |
| **Selected** [S] | **(b)**: §5. |
| **Reason** | Phase 9 needs one checklist. Each entry states what the offline engine assumes, so an evidence answer maps directly to a configuration change or a design change. |
| **State transitions** | None directly. Each entry names the rows it affects. |
| **Database** | None. |
| **Required tests** | A FakeKite model per entry where modellable (§5 column); the S3 harness adds the missing ones (placement refusal before visibility, DDPI-rejected trigger order, both repeated-modify/delete semantics, deleted GTTs pruned from the list, multi-page or partial listing). |
| **Blocks** | Offline engineering: **no** (every entry has a fail-closed posture). Live: **yes** for the entries marked so in §5. |

---

## 4. Consolidated design

### 4.1 Amended dark DDL (documentation only; applied in S1 after approval)

`PROTECTION_OPERATION_STATES` = `intent, sending, acknowledged, unknown, needs_review, confirmed, adopted, rejected,
not_sent, absent_after_settle, resolved_manual`. Open set unchanged:
`intent, sending, acknowledged, unknown, needs_review`.

```sql
CREATE TABLE protection_operations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    op_uuid               TEXT NOT NULL UNIQUE,
    account_id            INTEGER NOT NULL REFERENCES accounts(id),
    position_id           INTEGER NOT NULL REFERENCES trade_tickets(id),
    -- [R2-4] copied verbatim from trade_tickets.mode by the DD-5 INSERT ... SELECT; the ticket's non-NULL
    -- domain is exactly ('dry_run', 'live') (db.py:102), a NULL ticket mode yields no row; never execution_settings.mode
    mode                  TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    kind                  TEXT NOT NULL CHECK (kind IN ('create', 'modify', 'cancel')),
    reason                TEXT NOT NULL CHECK (reason IN ('fill', 'user_recreate',
                              'fill_resize', 'exit_fill', 'trailing', 'attribution_cap', 'user_modify',
                              'position_closed', 'supersede_cleanup', 'duplicate_cleanup', 'exit_prepare',
                              'user_cancel')),
    direction             TEXT CHECK (direction IS NULL OR direction IN ('up', 'down', 'tighten', 'loosen')),
    state                 TEXT NOT NULL CHECK (state IN (<the 11 states>)),
    basis_filled_quantity INTEGER NOT NULL CHECK (basis_filled_quantity >= 0),
    basis_exits_filled    INTEGER NOT NULL CHECK (basis_exits_filled >= 0),
    requested_quantity    INTEGER NOT NULL CHECK (requested_quantity >= 0),
    request_fingerprint   TEXT NOT NULL CHECK (json_valid(request_fingerprint)),
    request_payload       TEXT NOT NULL CHECK (json_valid(request_payload)),
    target_gtt_id         INTEGER,
    result_gtt_id         INTEGER,
    instance_epoch        INTEGER NOT NULL,
    attempts              INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    complete_reads_after  INTEGER NOT NULL DEFAULT 0 CHECK (complete_reads_after >= 0),
    superseded_at         TEXT,
    superseded_by_kind    TEXT CHECK (superseded_by_kind IS NULL OR
                              superseded_by_kind IN ('emergency_exit', 'exit', 'close', 'user')),
    superseded_by_ref     TEXT,
    resolution_id         INTEGER REFERENCES protection_operation_resolutions(id),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at               TEXT,
    acknowledged_at       TEXT,
    resolved_at           TEXT,
    last_checked_at       TEXT,
    evidence              TEXT CHECK (evidence IS NULL OR json_valid(evidence)),
    -- kind / reason / direction (DD-3, DD-4)
    CHECK ((kind = 'create' AND reason IN ('fill', 'user_recreate') AND direction IS NULL)
        OR (kind = 'modify' AND reason IN ('fill_resize', 'exit_fill', 'trailing', 'attribution_cap', 'user_modify')
                            AND direction IS NOT NULL)
        OR (kind = 'cancel' AND reason IN ('position_closed', 'supersede_cleanup', 'duplicate_cleanup',
                                           'exit_prepare', 'user_cancel') AND direction IS NULL)),
    CHECK (reason != 'fill_resize' OR direction = 'up'),
    -- [R3-1] an attribution_cap op is sent (first send or retry) only while DX classifies it caps_sell_exposure,
    -- re-proved at every send; never as none/redundant. Enforced by the engine's CAS, not expressible here.
    CHECK (reason NOT IN ('exit_fill', 'attribution_cap') OR direction = 'down'),
    CHECK (reason != 'trailing' OR direction = 'tighten'),
    -- retry bound (DD-4): only cancels and exposure-reducing modifies may be sent more than once
    CHECK (attempts <= 1 OR kind = 'cancel' OR (kind = 'modify' AND direction IN ('down', 'tighten'))),
    -- targets and results (Phase 1, kept)
    CHECK (kind = 'create' OR target_gtt_id IS NOT NULL),
    CHECK (kind != 'create' OR state NOT IN ('acknowledged', 'confirmed', 'adopted') OR result_gtt_id IS NOT NULL),
    -- sent_at (D§4.1, M-8)
    CHECK (state NOT IN ('intent', 'not_sent') OR sent_at IS NULL),
    CHECK (state NOT IN ('sending', 'acknowledged', 'confirmed', 'rejected', 'unknown', 'adopted',
                         'absent_after_settle') OR sent_at IS NOT NULL),
    CHECK (state != 'absent_after_settle' OR (kind = 'create' AND sent_at IS NOT NULL AND complete_reads_after >= 2)),
    -- overlay (Q1)
    CHECK ((superseded_at IS NULL) = (superseded_by_kind IS NULL)
       AND (superseded_at IS NULL) = (superseded_by_ref IS NULL)),
    CHECK (NOT (state = 'intent' AND superseded_at IS NOT NULL)),
    -- resolution (Q3)
    CHECK ((state = 'resolved_manual') = (resolution_id IS NOT NULL)),
    -- open vs terminal
    CHECK ((state IN ('intent', 'sending', 'acknowledged', 'unknown', 'needs_review')) = (resolved_at IS NULL))
);

-- at most one open exposure-changing op per position (D§3 Q3, kept)
CREATE UNIQUE INDEX idx_protection_ops_one_open_change ON protection_operations(position_id)
  WHERE kind IN ('create', 'modify') AND state IN ('intent', 'sending', 'acknowledged', 'unknown', 'needs_review');
-- at most one open cancel per (position, target GTT) (amends D§3 Q3; M-7)
CREATE UNIQUE INDEX idx_protection_ops_one_open_cancel ON protection_operations(position_id, target_gtt_id)
  WHERE kind = 'cancel' AND state IN ('intent', 'sending', 'acknowledged', 'unknown', 'needs_review');

CREATE TABLE protection_operation_resolutions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id            INTEGER NOT NULL UNIQUE REFERENCES protection_operations(id),
    outcome          TEXT NOT NULL CHECK (outcome IN ('linked_gtt', 'no_app_gtt', 'deleted_in_kite_verified')),
    linked_gtt_id    INTEGER,
    reason           TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    evidence_read_id INTEGER NOT NULL REFERENCES broker_reads(id),
    resolver         TEXT NOT NULL CHECK (length(trim(resolver)) > 0),
    instance_epoch   INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((outcome = 'linked_gtt') = (linked_gtt_id IS NOT NULL))
);
```

Unchanged from Phase 1:

- `idx_trade_tickets_unique_gtt`, with dark skip behaviour (DD-6);
- `trade_tickets.protection_state` with the 15 §3.3 names (DD-7).

The `ticket_events` suffix becomes the 13 types of DD-7.

Notes on the DDL:

- The two-way FK between ops and resolutions is legal in SQLite. The resolution row is inserted first, then the
  op's CAS sets `resolution_id`, in one fenced transaction.
- `broker_reads` is a v11 table, so the dark schema keeps requiring v11 (`_require_current_schema`).
- Not STRICT: P1-Q8 is unchanged. Paper ids are stored as text under INTEGER affinity.
- **Revision 2 changes no table, column, CHECK or index [R2-1…R2-7].** It adds only the `mode` comment above. The new
  effect class `caps_sell_exposure` is not stored (DX: effect classes are never columns). The R2-2 re-claim bound,
  the R2-5 timing and the R2-6 derivations are computed from existing columns and `evidence`.
- **Revision 3 changes no table, column, CHECK or index [R3-1…R3-3].** It adds only the comment on the
  `attribution_cap` CHECK. ΔS, ΔP (R3-1), the membership set 𝒢, G_max and S_agg (R3-2), and the grace timestamps (R3-3)
  are computed from existing columns, `broker_reads` and `evidence`; none is stored as a column.
- **Revision 4 changes no table, column, CHECK or index [R4].** The per-member worst-case quantity w(m) and S_agg
  (M-7 rev. 4) are computed from the existing `request_payload` (every SELL leg), `broker_reads` (listed versions,
  old modify configurations, triggered orders) and `evidence`. `requested_quantity` is unchanged, and is no longer
  the sole source of a multi-leg create's exposure. Nothing is stored as a column.
- **Revision 5 changes no table, column, CHECK or index [R5-1, R5-2].** An id-less create's UNKNOWN w (R5-1) and the
  condition (e) evidence (R5-2) are derived from the existing `result_gtt_id`, `request_payload`, `broker_reads`
  (listed GTTs, triggered orders) and `evidence`, plus the position's recorded fills. Nothing is stored as a column.
- **Revision 6 changes no table, column, CHECK or index [R6].** T(m) and O(m) are derived sets, computed from the same
  `broker_reads`, `evidence` and recorded fills as R5-2. Neither is stored as a column.

### 4.2 Amended transition table (replaces D§4.3; amendments in **bold**; revision 2 changes tagged [R2-n])

| # | From | Trigger | To | Auto-retry |
|---|---|---|---|---|
| 1 | — | Claim (`INSERT … SELECT`, DD-5): epoch, partition free; create/modify: no open cancel on the position, no exit in flight, no open op with overlay, attribution allows; **create: M-5 conditions; `increases_sell_exposure`: DD-9**; **automatic cancel: DX ∈ {none, redundant}**; **automatic `attribution_cap` modify: DX = `caps_sell_exposure` [R2-1]** **(evaluated before `none`; never claimed as `none` [R3-1])**; **automatic new cancel after a terminal `not_sent`/session-class `rejected` cancel on the same target: M-1 rev. 2 table and re-claim bound [R2-2]**; **`t.mode IS NOT NULL`, copied verbatim [R2-4]** | `intent` | n/a |
| 2 | `intent` | Pre-write check fails | `not_sent` | create: new op allowed automatically (M-5 cond. 3) |
| 3 | `intent` | Crash / restart (Stage 1 local inventory, fenced) | `not_sent` | as 2 |
| 4 | `intent` | Superseded | `not_sent` + overlay | no |
| 5 | `intent` | CAS `intent→sending` (`attempts=1`): overlay NULL, no exit in flight, epoch current, **DX re-evaluated, M-6 clock check, DD-8 freshness**; committed before the call. **[R2-7] M-6 gate on the shared `RESUMED_AFTER_SUSPEND` flag, not only on a gap; a second clock check immediately before the broker call (a gap there → no call, op stays `sending`, then row 10)**. **[R3-1] DX uses the rev. 3 precedence; an `attribution_cap` op re-proves all seven conditions against this send's read** | `sending` | n/a |
| 5a | `intent` | **Row 5 predicates fail at send time** | **`not_sent`** | as 2 |
| 6 | `sending` | Response with a result (M-4 context, same attempt) | `acknowledged` | n/a |
| 7 | `sending` | Definite rejection | `rejected` | **no automatic retry; exception per M-3 (a new op for a `none`-effect cancel after a session-class rejection)**. **[R2-2] The rejected op is never resent; the new op is bounded per (position, target)** |
| 8 | `sending` | Ambiguous exception | `unknown` | per row 16 |
| 9 | `sending` (own epoch) | Within `PROTECTION_INFLIGHT_GRACE_SECONDS` | unchanged | n/a |
| 10 | `sending` (other epoch, or own past the grace) | Stage 1 / recovery | `unknown` (**settle count reset**) | per row 16 |
| 11 | `acknowledged` | Read-back matches | `confirmed` | n/a |
| 12 | `acknowledged` | Read-back contradicts (wrong instrument, BUY leg, non-CNC, id linked elsewhere) | `needs_review` | no |
| 13 | `acknowledged` | Read unusable | unchanged | read only |
| 14 | `unknown` (create) | Exactly one fingerprint match, unlinked, no other unlinked active SELL GTT, complete read | `adopted` (link CAS, DD-6 guard) | n/a |
| 15 | `unknown` (modify/cancel) | Read shows target values / **explicit `deleted`/`cancelled`** | `confirmed` | n/a |
| **15a** | `unknown` (cancel) | **Target missing after the settle rule, or listed `triggered` or in an unknown status** | **`needs_review`** | no |
| **15b** | `unknown`/`acknowledged` (modify) | **Target missing, `triggered` or in an unknown status** | **`needs_review`** (or `rejected` on a definite refusal) | no |
| 16 | `unknown` (cancel; modify `down`/`tighten`) | **M-3 conditions 1–3** (post-attempt read shows the pre-state; DX ∈ {none, redundant} **or, for `attribution_cap` only, `caps_sell_exposure` [R2-1], re-proved against this retry's read and never satisfied as `none` [R3-1]**; backoff; cap). **[R2-2] This is the only same-op retry; a cancel whose effect became `none` is retried here only if it is `unknown`** | `sending` (`attempts+1`) | yes, bounded |
| 17 | `unknown` (**create only**) | Settle rule (DD-8). **[R2-5] Earliest: `sent_at` + 180 s plus two read latencies (placeholders)** | `absent_after_settle` | never |
| **17c** | `unknown` | **Late response, same attempt (M-4)** | **`acknowledged` / `rejected`** | per kind |
| 18 | `unknown` | > 1 match; an unlinked active SELL GTT; id linked elsewhere | `needs_review` | no |
| 19 | `sending`/`acknowledged`/`unknown` (create/modify) | Superseded | unchanged + overlay | per kind |
| 20 | open + overlay | Resolution reveals a GTT id | per **§4.3** | per §4.3 |
| 21 | open + overlay | Resolution yields no GTT | `rejected` / `not_sent` / **`absent_after_settle` (create only)** | none |
| 22 | `needs_review` | Human resolution with a fresh complete read (Q3). **[R2-6] Position `protection_state` re-derived from the evidence read; never `PROTECTION_CONFIRMED` by outcome alone** | `resolved_manual` | n/a |
| 23 | `absent_after_settle` | A late matching GTT observed | unchanged; `LATE_GTT_APPEARED` | no |
| 24 | any open | Process fenced | unchanged; the successor resolves | — |
| **25** | any, at a `→sending` CAS | **Clock gap detected (M-6), or `RESUMED_AFTER_SUSPEND` set by an earlier detector [R2-7]** | **refused; in-CAS memory updates only (`resume_seq`, reason, `resumed_at`); `note_resume` and the G4 reset are deferred [R2-7]; op unchanged (`intent` → `not_sent`)** | — |
| **26** | terminal | **Late response, consistent** | **unchanged; evidence** | — |
| **27** | terminal / non-current attempt | **Late response, contradictory or stale** | **unchanged; finding (M-4)** | — |

**Never allowed:**

- any transition out of a terminal state;
- `unknown→sending` for a create, modify `up` or modify `loosen`;
- `intent→sending` with the overlay set;
- any write by a non-current epoch;
- any send whose DX class does not permit it;
- any transition driven by an incomplete read;
- **[R2-2]** resending a terminal op (`rejected`, `not_sent`, …): a follow-up is always a new op, under the M-1 rev. 2 table;
- **[R2-1]** an `attribution_cap` send whose `caps_sell_exposure` conditions were not re-proved at that send;
- **[R2-3]** an Emergency Exit sale while G_max > 0 (M-7 rev. 2);
- **[R3-1]** classifying an `attribution_cap` quantity decrease as `none` or `redundant`;
- **[R3-2]** an Emergency Exit sale unless the instrument's set 𝒢 is established with G_max = 0 **and** S_agg = 0
  (M-7 rev. 3); merging two different GTT ids; capping S_agg at holdings;
- **[R3-3]** an Emergency Exit sale while any member's cancel is UNKNOWN, while R_EE lists a member `ACTIVE` (even
  after an accepted or `confirmed` cancel), or on a cancel acknowledgement alone (M-7 rev. 3 grace-outcome table);
  any "sell anyway" override or deferred sale;
- **[R4]** in S_agg (M-7 rev. 4): treating two SELL legs, or two outstanding triggered SELL orders, as mutually
  exclusive; taking a multi-leg create's exposure from `requested_quantity` alone; counting only one of the old and
  requested configurations of an unresolved modify, or summing alternative versions of one GTT as if they were
  separate GTTs; dropping a member's exposure on a cancel acknowledgement alone.
- **[R5-1, R5-2]** in S_agg and 𝒢 (M-7 rev. 5): presenting an id-less create's payload quantity as its worst case or
  as a bound; treating one recorded id as proof that a create produced only one GTT; merging id-less entries;
  removing a triggered multi-leg member, or dropping its remaining exposure, because one of its triggered orders is
  terminal with zero remaining quantity.
- **[R6]** in 𝒢 and S_agg (M-7 rev. 6): removing a triggered member on the evidence of O(m) alone (an empty
  outstanding-order list, or visible terminal orders) while T(m) is not established as complete; omitting an expected
  but not-yet-visible triggered order from T(m); counting a proven terminal order as outstanding sell exposure.

### 4.3 Resolution of a superseded op that reveals a GTT id (replaces D§4.3 row 20; Q1 c)

Evaluated in the resolution's fenced transaction, against the position at that moment:

| Position at resolution | Other linked GTT? | Action | Effect class of any cancel |
|---|---|---|---|
| Broker-proven flat | any | Record the id (`confirmed`/`adopted`); claim a `supersede_cleanup` cancel for it | `none`: automatic, bounded retry |
| Open, exit in flight (an exit owner exists) | any | Record; claim a `supersede_cleanup` cancel | `reduces_protection` inside the exit claim: one attempt; becomes `none` once flat. **[R2-2] Once flat: `unknown` → same-op retry (row 16); `not_sent` → new op; `rejected` → new op only after a session-class rejection; otherwise nothing automatic (M-1 rev. 2)** |
| Open, no exit in flight | none | Record and **link as the position's protection** if the shape fits (SELL, instrument, CNC, qty ≤ open qty). The overlay lapses (evidence plus event). Shape does not fit → `needs_review` | — |
| Open, no exit in flight | yes | Id app-proven (from a response) → `duplicate_cleanup` under M-2; adopted-by-fingerprint → `needs_review` | `redundant` only under M-2 |

### 4.4 Retry classes v2 (replaces D§4.2)

| Kind / direction / reason | Effect class (typical) | First claim | Automatic retry |
|---|---|---|---|
| create (`fill`) | `increases_sell_exposure` | automatic, under DD-9 + M-5 | **never** |
| create (`user_recreate`) | `increases_sell_exposure` | user only | never |
| modify `up` (`fill_resize`, `user_modify`) | `increases_sell_exposure` | automatic `fill_resize` under DD-9 | never |
| modify `loosen` (`user_modify`) | `reduces_protection` | user only | never |
| modify `down` (`exit_fill`) to exactly the open quantity | `none` | automatic | yes, bounded (M-3) |
| modify `down` (`attribution_cap`) ~~below the open quantity~~ **[R3-1] to Q_cap, where Q_cap ≤ the open quantity and Q_cap < the current GTT quantity** | ~~`reduces_protection`, but reduces sell exposure (D§6.3)~~ **`caps_sell_exposure` [R2-1]** (DX rev. 2, conditions 1–7; **[R3-1] classified before `none`, so also when Q_cap equals the open quantity; when Q_cap is below it, up to open − Q_cap shares lose coverage and the position goes to `PROTECTION_NEEDS_REVIEW`**); if any condition fails: `reduces_protection`, not automatic. **[R3-1] A GTT already at or below Q_cap needs no cap: no op (`none`, or a resize-up refused under `broken`)** | automatic, with an URGENT finding | yes, bounded, **only while the attribution evidence still holds at each send** (M-3 read-back; Q_cap re-computed; Q_cap = 0 → needs review, no cancel) |
| modify `tighten` (`trailing`) | `none` | automatic | yes, bounded |
| cancel `position_closed` / `supersede_cleanup` on a flat position | `none` | automatic | yes, bounded (same op from `unknown` only; after a terminal op, a new op per M-1 rev. 2 [R2-2]) |
| cancel `duplicate_cleanup` | `redundant` (M-2) or not automatic | automatic under M-2 | yes, bounded, while M-2 holds |
| cancel `exit_prepare` on an open position | `reduces_protection` | within the exit claim | **no** while open; ~~yes once flat~~ **once flat: per the M-1 rev. 2 state table [R2-2]** |
| cancel `user_cancel` | `reduces_protection` (if open) | user only | **no** while open; ~~yes once flat~~ **once flat: per the M-1 rev. 2 state table [R2-2]** |

~~`attribution_cap` is the one automatic `reduces_protection` case.~~ **[R2-1]** No `reduces_protection` op is automatic
while the position is open. `attribution_cap` is automatic only as `caps_sell_exposure` (DX rev. 2), a distinct
effect class. It is kept from D§6.3, because an over-quantity GTT risks a short sale (B-04). It is reported, not
silent. **[R3-1]** It is evaluated before `none` (DX rev. 3), re-proved at every send, and never described as full
protection when Q_cap is below the open quantity.

---

## 5. Broker-dependent assumption register (M-10)

Legend for **Blocks**:

- **LIVE-BLOCKER**: must be answered before activation (P§13 set, U1, and the facts this design's sufficiency
  rests on);
- **LIVE-P9**: a Phase 9 validation item. The engine is safe without it; an answer may relax a fail-closed path
  or set a parameter;
- **NONE**: disclosed residual.

No entry blocks offline engineering.

**Revision 2 changed no classification [R2-1…R2-7].** It updated the offline posture of B-04, B-08, B-17 and B-30, as
tagged in the rows. B-04 stays **LIVE-BLOCKER**. Emergency Exit's sale no longer relies on it (M-7 rev. 2), but it
still underpins `caps_sell_exposure` (including the Q_cap = 0 residual), an over-quantity GTT after an undetected
manual sale, and any future relaxation of the M-7 sale gate.

**Revision 3 changed no classification [R3-1…R3-3]; all 33 entries keep their Blocks value.** It updated the offline
posture text of B-04, B-24, B-28 and B-31, as tagged in the rows.

**Revision 4 changed no classification [R4]; all 33 entries keep their Blocks value.** It updated the offline posture
text of B-24 only, as tagged in the row.

**Revision 5 changed no classification [R5-1, R5-2]; all 33 entries keep their Blocks value.** It updated the offline
posture text of B-24 only, as tagged in the row.

**Revision 6 changed no classification and no posture text [R6]; all 33 entries keep their Blocks value.** The B-13
posture (a trigger without a visible order → `BROKER_DATA_INCONSISTENT`) is consistent with R6: such an order is an
expected member of T(m), so the evidence is incomplete and the member stays in 𝒢.

| # | Assumption / question (source) | Offline posture of the engine | FakeKite model | Resolving evidence | Blocks |
|---|---|---|---|---|---|
| B-01 | Kite deduplicates identical GTTs? (P§10.2) | Assume **no**: fingerprint plus "no other unlinked SELL GTT" | no dedup | Docs / support | LIVE-P9 |
| B-02 | A CNC SELL GTT can be placed seconds after a fill, before holdings/positions show the shares (P§10.3) | Placement refusal = `rejected`. Position stays `PROTECTION_PENDING` with a visible gap. A new create needs a new op after the pre-write check | to add (S3) | Supervised one-share trade (Phase 9) | **LIVE-BLOCKER** |
| B-03 | GTT trigger and execution depend on holdings availability at trigger time (P§10.4) | A day-0 GTT is shown as "protective value unverified"; never presented as proven protection until answered | `trigger_gtt_against_holdings(policy)` (`fake_kite.py:199` [V via RR§9]) | Docs plus Phase 9 | **LIVE-BLOCKER** |
| B-04 | Over-quantity triggered GTT: rejected, partial, or short (P§10.5) | ~~EE sells under the exclusive-only rule~~ **[R2-3] EE never sells while any GTT that could still sell the position is not confirmed gone (G_max = 0, M-7 rev. 2); exclusive attribution is necessary, never sufficient**; **[R3-2] the instrument-wide set 𝒢 must be established with G_max = 0 and S_agg = 0; S_agg is never capped at holdings, and two SELLs are not assumed unable to hit the same shares**; `POSSIBLE_DOUBLE_SELL` watch after every EE sale; `attribution_cap` as `caps_sell_exposure` (**[R2-1]**; Q_cap = 0 → needs review, residual; **[R3-1]** evaluated before `none`; Q_cap below the open quantity → partial coverage, needs review) | policy `reject` (default) | Docs only; **never tested live** | **LIVE-BLOCKER** |
| B-05 | DDPI/TPIN for unattended GTT-triggered CNC sells (P§10.6, U1) | "Triggered, order rejected, position open" → surfaced as `PROTECTION_FAILED`/`NEEDS_REVIEW`; never treated as protected or closed | to add (S3): trigger with order `REJECTED` (`fake_kite.py:160` [V via RR§8]) | Docs; the user's read-only check of DDPI status | **LIVE-BLOCKER** |
| B-06 | T1 shares sellable by a GTT; auction risk (P§10.7) | Attribution counts T1 as sellable (today's `broker_holdings`); unverified | holdings T1 field | Docs | LIVE-P9 |
| B-07 | `modify_gtt` keeps the id, is atomic, refused after trigger (P§10.8) | Read back the target id. Id gone → `needs_review` (row 15b) | modify keeps id | Docs plus Phase 9 one-share GTT | LIVE-P9 |
| B-08 | `get_gtts()` read-after-write lag and list completeness, including **no pagination** (P§10.9, U8) | Settle counts complete reads, not time; absence is inference with a watch; cancel-missing → `needs_review`. **[R2-5]** With the placeholders, the earliest `absent_after_settle` is `sent_at` + 180 s plus two read latencies (DD-8 rev. 2); Phase 9 sets the real values | listing lag N (`fake_kite.py:73` [V via RR§9]); a partial-listing model to add (S3) | Phase 9 repeated reads | **LIVE-BLOCKER** (sets DD-8) |
| B-09 | GTT `created_at`/`updated_at`/`expires_at`; `condition.last_price` echoed (P§10.10) | The fingerprint excludes these fields | none | Phase 9 read-only `get_gtts` | LIVE-P9 |
| B-10 | Retention of non-active GTTs in the list (P§10.11) | Watch window 30 days (placeholder); cancel-missing not confirmed | to add: deleted GTTs pruned | Phase 9 read-only | LIVE-P9 (sets DD-8) |
| B-11 | GTT expiry (P§10.12) | `expired` → `PROTECTION_FAILED`, re-create by the user only (M-5) | to add: status `expired` | Docs | LIVE-P9 |
| B-12 | Corporate actions cancel or adjust GTTs (P§10.13) | Any non-app status change → `PROTECTION_FAILED`/`NEEDS_REVIEW` | none | Docs | LIVE-P9 |
| B-13 | A triggered GTT's order link persists; `order_history` for an earlier day (P§10.14) | Trigger without a visible order → `BROKER_DATA_INCONSISTENT` (Stage 1, Ph2§7 [V]) | trigger sets order | Phase 9 read-only | LIVE-P9 |
| B-14 | `positions()` visibility latency after a fill (P§10.15) | `FILL_VISIBILITY_TOLERANCE_SECONDS = 60`; attribution `unknown` meanwhile | stale snapshots | Phase 9 | LIVE-P9 |
| B-15 | API session expiry does not affect GTTs (P§10.16) | GTTs are assumed to persist server-side; observations are UNVERIFIED after a restart, never absent | n/a | Docs | NONE |
| B-16 | Rate limits; maximum active GTTs; **the error class of a rate-limit refusal** (P§10.17) | A refusal that is not a definite class → `unknown` (never re-sent; adopt or settle) | `raise_on_next` | Docs | LIVE-P9 |
| B-17 | Error type of deleting an already deleted or triggered GTT (P§10.18) | Cancel retries only after a read shows `active` (M-3), so the error type never drives a retry. **[R2-2]** A refusal saying the GTT does not exist, while a fresh read lists it `active` → URGENT finding, no automatic re-claim | `InputException` for unknown ids | Phase 9 | LIVE-P9 |
| B-18 | LIMIT leg fill on an illiquid gap (P§10.19) | Disclosed unprotected interval | n/a | Market behaviour | NONE |
| B-19 | T2T/BE, ASM/GSM eligibility for GTT/BTST (P§10.20) | Entry eligibility, not the engine | n/a | Docs | LIVE-P9 |
| B-20 | KiteTicker `on_order_update` guarantees (P§10.21) | Not used | n/a | Docs | NONE |
| B-21 | A second-client login invalidates the first token (U5) | Non-owner login is refused regardless | n/a | Docs | NONE |
| B-22 | Squid: no `ssl_bump`; `forward_max_tries`/`retry_on_error` cannot replay (U6M§8.1) | Client-side resend closed (M1, `max_redirects=0`); proxy replay unverified | n/a | VM owner config review | **LIVE-BLOCKER** |
| B-23 | Kite 3xx on writes (U6M§8.2) | Ends `unknown` (never resent) | recorded transport | Docs | NONE (availability) |
| B-24 | Kite's edge retrying to its OMS, so one request could create two GTTs (U6M§8.3) | Two fingerprint matches → `needs_review` (row 18); never auto-adopt or auto-delete. **[R3-2]** In the EE gate an id-less create counts as a separate member with ~~its requested quantity~~ **[R4] (superseded by R4) the sum of every SELL leg in its `request_payload`** ~~as a lower bound~~, never merged with a listed id. **[R5-1] (superseded by R5)** An id-less create whose resulting GTTs are not all identified has w UNKNOWN, so S_agg is UNKNOWN. Its payload quantity is never a worst case or a bound. Each independently identified resulting GTT is a separate member, and one recorded id never proves a single GTT | `on_call` duplicate injection (S3) | Support | LIVE-P9 (disclosed residual) |
| B-25 | No idempotency key; `tag` dedup unknown (U6M§8.4) | Assume no dedup | none | Docs | NONE (design assumes the worst) |
| B-26 | 5xx semantics: a GTT may exist after `NetworkException`/`GeneralException`; whether `OrderException` can accompany partial processing (U6M§8.5) | 5xx = ambiguous. A `rejected` create still leaves the next create gated by M-5 cond. 4 (no unlinked SELL GTT), which catches a hidden GTT | `accept_then_raise` | Docs / support | LIVE-P9 |
| B-27 | Repeated `modify_gtt`/`delete_gtt`: no-op or error (U6M§8.6, G5) | Re-send only after a read shows the pre-state (M-3), so either semantics is safe | to add: both modes (S3) | Phase 9 | LIVE-P9 |
| B-28 | **New:** deleted/cancelled GTTs remain listed with an explicit status long enough to confirm a cancel | Assume **not**: missing ≠ confirmed (row 15a). **[R3-3]** A member missing from R_EE is never removed from the EE set (grace-outcome rows G5, G9, G12) | to add: pruning | Phase 9 | LIVE-P9 (may relax row 15a) |
| B-29 | **New:** GTT ids are never reused | Ids are keys for linking, the unique index and M-4 evidence | ids monotonic | Docs / support | LIVE-P9 |
| B-30 | **New:** `TokenException` on delete is a definite refusal (C4, `gtt_cancellation.py:39` [V]), while on create it is ambiguous (D§4.3 row 8) | A definite delete refusal changes only retry eligibility; confirmation still needs a read. **[R2-2]** A session-class rejection allows at most one automatic **new** cancel op per (position, target) per session, and only when its effect is `none` | `expire_session` | Docs | LIVE-P9 |
| B-31 | **New:** a request can be processed after the client timeout; SDK timeout 7 s (D§2.4) | In-flight grace = timeout + 5 s; late responses per M-4. **[R3-3]** The EE wait runs from the last relevant cancel sent in the click, so each cancel gets the full grace | `on_call` delay | Docs / support | LIVE-P9 (sets the grace) |
| B-32 | **New:** the GTT status vocabulary (`active`, `triggered`, `disabled`, `expired`, `cancelled`, `deleted`, `rejected`, …) | Only `active` = armed; only `deleted`/`cancelled` = cancel confirmed; any other or unknown status → `needs_review`, never proof | statuses used in FakeKite | Phase 9 read-only | LIVE-P9 |
| B-33 | **New:** a broker-proven flat position cannot be re-opened by a pending GTT (it can only sell) | Cleanup cancels on flat positions are `none`-effect | n/a | Docs (follows from SELL-only legs) | NONE |

**Non-broker environmental assumption** (listed for completeness): `time.monotonic()` behaviour across sleep
(U3) was answered for one laptop only (WIN §3.4). DD-8 and M-6 use both clocks and never trust the monotonic
clock alone.

---

## 6. Amendments to earlier documents (applied only by this document's approval)

| # | Earlier text | Amendment | Decision |
|---|---|---|---|
| A-1 | D§3 Q3 (D:371): one open cancel **per position** | One open cancel per **(position, target GTT)** | M-7 |
| A-2 | D§4.2 (D:446): cancel always auto-retried | Retry by effect class (DX); cancel only when `none`/`redundant` | M-1, DX |
| A-3 | D§4.3 row 7: modify-down/cancel "yes (bounded)" after `rejected` | No automatic retry after `rejected`; one narrow session-class exception as a new op | M-3 |
| A-4 | D§4.3 row 15: confirmed on "deleted or cancelled" | Only an **explicit** status; missing → row 15a (`needs_review`) | M-3 |
| A-5 | D:319 and row 21: `absent_after_settle` for any kind | Create only (D:1145 governs) | M-8 |
| A-6 | D§4.3 row 20: always link then cancel | Re-derive the action from the position state (§4.3) | Q1 |
| A-7 | D:1148: resolution outcome includes `other`; `evidence_read_id` nullable | `other` removed; `evidence_read_id NOT NULL`; `op_id UNIQUE` | Q3 |
| A-8 | D:1150: `direction ∈ (up, down, neutral)` | `(up, down, tighten, loosen)`, riskiest component | DD-4 |
| A-9 | D:1152: two new event types | Seven new event types (13 in total) | DD-7 |
| A-10 | RR DD-3 list (`cleanup`, …) | The per-kind list of DD-3 (`cleanup` → `position_closed` / `duplicate_cleanup`) | DD-3 |
| A-11 | D§8.4 (D:943): "cancel retries" as a permitted Stage 2 class | Only `none`/`redundant` cancels | M-1 |
| A-12 | RR§10 G1: engine claims only | Plus the legacy-path detection as a live-activation blocker | M-6 |
| A-13 | D:278, D:474: position "stays `CLOSED_CLEANUP_REQUIRED`" (stored) | Derived, never stored | DD-7 |
| **A-14 [R2-3]** | D§3 Q1.6 (the EE sell decision) and P§7.3 ("EE is never blocked by a protection op") | The sale requires G_max = 0: no GTT that could still sell the position is unresolved. Exclusive attribution is necessary, never sufficient. "Never blocked" applies to the journal (claim, supersede, cancels), not to a sale against a GTT that may still be armed. **[R3-2, R3-3]** "Unresolved" means a member of the complete instrument-wide set 𝒢; the sale needs G_max = 0 and S_agg = 0, both established, judged by the grace-outcome table | M-7 |
| **A-15 [R2-7]** | RR§10 G4 and this document's rev. 1 DD-10/M-6: `note_resume`, or the detecting CAS, calls `reset_cycle_memory()` | Consumer-driven reset keyed on `resume_seq`, under `CLASSIFICATION_LOCK`. `run_heartbeat_loop` runs `note_resume` for any unconsumed `resume_seq` | DD-10, M-6 |
| **A-16 [R2-1]** | D§4.2 row "modify, quantity down … yes, bounded", as applied to the D§6.3 resize-down under `broken` | Automatic only as `caps_sell_exposure` (DX rev. 2, conditions 1–7, re-proved at each send). **[R3-1]** Classified before `none`, also when Q_cap equals the open quantity | DX, §4.4 |

---

## 7. Conclusion

### 7.1 Per-decision status

"Approved" means **selected in this document; it takes effect when the user approves this document**. No decision
is in force yet.

| Decision | Status | Blocks offline engineering? | Blocks live activation? | Revisions 2–6 |
|---|---|---|---|---|
| DX dual exposure | Selected | until approved (S1/S4) | no | **changed** R2-1 (fifth class `caps_sell_exposure`, precedence); **R3-1** (independent axes; `caps_sell_exposure` evaluated before `none`) |
| DD-1 amended DDL | Selected | **yes: it is S1** | no | unchanged (no DDL change in rev. 2) |
| DD-2 v12 dark | Selected | no | **yes** (registration gate) | unchanged |
| DD-3 reason list | Selected | until approved (S1) | no | unchanged |
| DD-4 direction / retry CHECK | Selected | until approved (S1) | no | **changed** R2-1 (row 16 wording; CHECK unchanged); **R3-1** (note: never sent as `none`; CHECK unchanged) |
| DD-5 CAS-only, `INSERT … SELECT` | Selected | until approved (S4) | no | **changed** R2-4 (mode verified; one UNVERIFIED data fact, fail-closed) |
| DD-6 duplicate GTT ids | Selected | no | **yes** (preflight at registration) | **changed** R2-4 (preflight counts NULL-mode open positions) |
| DD-7 derived cleanup, `updated_at`, 13 events | Selected | until approved (S1) | no | **changed** R2-6 (manual resolution discharges cleanup) |
| DD-8 parameters | Selected (placeholders) | no | **yes** (values need Phase 9) | **changed** R2-5 (earliest time; spacing on recorded timestamps) |
| DD-9 exposure predicate | Selected | until approved (S4/S8) | no | unchanged |
| DD-10 G1/G2/G4 | Selected | until approved (S5/S8) | via M-6 | **changed** R2-7 (G4 reset is consumer-driven) |
| DD-11 dry run = tests | Selected | no | no | unchanged |
| Q1 overlay sub-choices | Selected | until approved (S7) | via M-7 | unchanged (§4.3 row 2 effect cell refined by R2-2) |
| Q2 settle sub-choices | Selected | until approved (S1) | via DD-8 | **changed** R2-5 (note only) |
| Q3 resolution sub-choices | Selected | until approved (S1/S6) | Phase 8 UI (separate) | **changed** R2-6 (protection state per outcome) |
| M-1 cancel not auto-reducing | Selected | until approved (S4/S7) | no | **changed** R2-1, R2-2 (state table); **R3-1** (reason text: partial coverage when Q_cap < open) |
| M-2 duplicate vs sole GTT | Selected | until approved (S6/S7) | no | unchanged |
| M-3 no blind retries | Selected | until approved (S5) | no (fail-closed; B-28 may relax) | **changed** R2-2 (exception stated precisely); **R3-1** (condition 1 aligned with row 16) |
| M-4 late-response association | Selected | until approved (S5) | no | unchanged |
| M-5 no create on temporary absence | Selected | until approved (S6/S8) | no | **changed** R2-6 (cond. 3) |
| M-6 resume gating | Selected | until approved (S8) | **yes** (legacy-path extension) | **changed** R2-1, R2-7 (concurrency); **R3-1** (a cap is never a `none` send after a resume) |
| M-7 Emergency Exit coordination | Selected | until approved (S7) | **yes** (Phase 6 wiring; B-04 no longer relied on by the sale, R2-3) | **changed** R2-2, R2-3 (sale gate G_max = 0); **R3-2** (complete set 𝒢, deduplication, G_max count and S_agg quantity); **R3-3** (grace start, outcome table, no override); **R4** (S_agg per-member quantity: SELL legs summed per version, maximum across alternative versions, outstanding triggered orders summed; gate unchanged); **R5-1** (id-less create whose resulting GTTs are not all identified → S_agg UNKNOWN; payload quantity never a bound); **R5-2** (condition (e) and G6: every triggered SELL order and SELL leg of a triggered GTT must be proven unable to execute; gate unchanged); **R6** (T(m) the complete triggered-order set vs O(m) its outstanding subset; condition (e) and G6 evaluate the completeness of T(m); O(m) feeds only the R4 exposure term; test T6; R4 formula and gate unchanged) |
| M-8 `absent_after_settle` CHECK | Selected | until approved (S1) | no | unchanged |
| M-9 v12 renumbering / compatibility | Selected | **yes** (mechanical, S1) | **yes** (registration) | unchanged |
| M-10 broker assumption register | Selected | no | **yes** (LIVE-BLOCKER entries) | **changed** postures of B-04/B-08/B-17/B-30; **R3** postures of B-04/B-24/B-28/B-31; **R4** posture of B-24; **R5-1** posture of B-24; **R6** none; no classification changed |

### 7.2 Summary table

| Approved decisions | Unresolved decisions | Engineering blockers | Live-activation blockers |
|---|---|---|---|
| All 25 entries in §7.1 (DX, DD-1…DD-11, Q1–Q3, M-1…M-10) are **selected and ready for approval**. None is in force until the user approves this document. | ~~**No design decision is left open.**~~ **[R2] No design decision that engineering needs is left open.** ~~One optional product question is open: an Emergency Exit user override (M-7 rev. 2, §7.3).~~ **[R3-3] Closed: no override (M-7 rev. 3).** One data fact is UNVERIFIED and handled fail-closed: NULL-mode open positions in real databases (DD-5 rev. 2). What remains open is broker evidence that sets values or may relax fail-closed paths: <br>• the real DD-8 values (B-08, B-10, B-31); <br>• cancel-missing semantics (B-28, row 15a); <br>• Kite error classes for rate limits, repeated modify/delete and delete of a gone GTT (B-16, B-17, B-27, B-30); <br>• GTT status vocabulary and id reuse (B-29, B-32); <br>• edge retries to the OMS (B-24). <br>Also: whether to add triggers at registration (DD-5, revisit). | 1. **User approval of this document (G0).** <br>2. **S1**: apply the §4.1 DDL to the dark schema; renumber to `_m012_*`/`_r012_*`; update the test pins (`:194`, `:688`, and the old-enum cases). <br>3. **S3 harness** before S4: shared-state multi-process FakeKite; day rollover; clock injection; the FakeKite models listed in §5 (placement refusal, DDPI-rejected trigger, repeated-op semantics, pruning, partial listing, duplicate injection). <br>Inside Phase 3, before the engine counts as complete (RR§13): G3 journal-aware Stage 1, G1/M-6 engine gate, G2, G4, F2 test. <br>**[R2-7]** S8 includes the `instance_lock` changes: the shared baseline, `resume_seq`, and `run_heartbeat_loop` consuming an unnoted `resume_seq`. Also the consumer-driven G4 reset in `reconciliation`/`startup_reconciliation`. None of these files is PK-25 frozen [V: manifest `model_freeze`]. <br>**[R3]** The engine's DX classifier implements the rev. 3 precedence (S4), and the S7 EE API implements the 𝒢 / G_max / S_agg gate (**[R4]** S_agg per the M-7 rev. 4 row; **[R5-1, R5-2]** an id-less create's UNKNOWN w and condition (e) rev. 5, tests T4–T5; **[R6]** condition (e) rev. 6 over the complete triggered-order set T(m), test T6) and the grace-outcome table. Neither adds a blocker beyond S4 and S7. | 1. **U1 DDPI/TPIN** (B-05). <br>2. **P§10 items 3, 4, 5, 9** (B-02, B-03, B-04, B-08). <br>3. **Proxy replay** config review (B-22) and the remaining U6M§8 items (B-24, B-26, B-27: Phase 9). <br>4. **Phase 4** frozen `execution.py` change (separate authorisation, PK-25 supersede record). <br>5. **Phase 6** Emergency Exit / exit wiring (M-7). <br>6. **M-6 legacy-path resume detection** (`loop_may_run`, `broker_write`, `confirm_time_blockers`, `sync_protection` resize-up). <br>7. **v12 registration** (DD-2), with the operator backup (Ph2§11, not done) and the DD-6 preflight, **including the NULL-mode open-position count [R2-4]**. <br>8. **Phase 9** broker validation and the real DD-8 values. Any relaxation of the M-7 sale gate needs B-04 evidence **[R2-3]**. **Phase 10** gate decision. <br>9. **WIN-INT** L3-subst, L6 remote share, R8 real sleep with the runner fix (RR§11.1). <br>10. `LIVE_ENTRIES_ENABLED` stays `False` until all of the above. |

**Phase 3 engineering:** may begin at S1 once this document is approved.
**Live trading:** **NO-GO**, unchanged.

### 7.3 Revision 2 conclusion

**Decisions now consistent.** Each group below now states one rule, in every place listed:

| Topic | Places that now agree | The single rule |
|---|---|---|
| `attribution_cap` automation (R2-1) | DX rev. 2, DD-4 (row 16 text), M-1 (reason), M-6 (gate list), §4.2 rows 1/16 and "never allowed", §4.4, §5 B-04, A-16 | `caps_sell_exposure` is a distinct effect class: automatic claim and bounded retry, all seven conditions re-proved at each send. `reduces_protection` is never automatic while the position is open. **[R3-1] The rev. 2 precedence (`none` before the cap) is superseded; see §7.4.** |
| A cancel that becomes `none` once flat (R2-2) | M-1 rev. 2 table, M-3 rev. 2, M-7 step 8, §4.2 rows 1/7/16 and "never allowed", §4.3 row 2, §4.4, §5 B-17/B-30 | Same-op retry only from `unknown`. Terminal ops are never resent. A new op after `not_sent`, or after a session-class `rejected`, is bounded per (position, target). Nothing automatic after any other rejection. |
| Emergency Exit sale (R2-3) | M-7 rev. 2 (steps 4–6, 8), §4.2 "never allowed", §5 B-04, A-14 | Sell only when G_max = 0. Exclusive attribution is necessary, never sufficient. The B-04 relaxation is deferred. **[R3-2, R3-3] The rev. 2 G_max definition and the step 4 wait are superseded; see §7.4.** |
| `mode` (R2-4) | DD-5 rev. 2, DD-6 rev. 2, §4.1 comment, §4.2 row 1 | Verified domain `('dry_run','live')`, NULL until the claim. The op copies it verbatim, identity mapping. NULL → no row. Never the global mode. |
| Settle timing (R2-5) | DD-8 rev. 2, Q2 note, §4.2 row 17, §5 B-08 | T0 = max(`sent_at` + SETTLE, `unknown_at`, `reset_at`). Spacing from R1 `gtts_at` to R2 `started_at`. Earliest: `sent_at` + 180 s plus latencies. |
| After a manual resolution (R2-6) | Q3 rev. 2, DD-7 rev. 2, M-5 cond. 3, §4.2 row 22 | The state is re-derived from the evidence read and never confirmed by the outcome alone. "No GTT" outcomes → `PROTECTION_FAILED` and `user_recreate`. On a flat position, cleanup is discharged with a watch. |
| Resume concurrency (R2-7) | M-6 rev. 2, DD-10 rev. 2, §4.2 rows 5/25, A-15 | One shared baseline under `cv`, gating on the shared flag. Only memory updates inside the CAS. Deferred `note_resume`; the G4 reset is consumer-driven. Pre-call re-check; residual disclosed. |

**Decisions still unresolved.**

- **No design decision that Phase 3 engineering needs is unresolved.**
- ~~**One product question is open, for the user:** should Emergency Exit offer an explicit per-click override ("sell anyway,~~
  ~~I accept the double-sell risk") when the G_max gate refuses a sale (M-7 rev. 2)? If unanswered, there is **no**~~
  ~~**override** (fail-closed). This does not block S1–S8.~~ **[R3-3] Superseded: closed as "no override" (M-7 rev. 3).**
- **Revisit items** (not blocking): SQLite triggers at registration (DD-5); a `sending → not_sent` "provably not
  sent" transition for a pre-call abort (M-6 rev. 2, point 8); relaxing the M-7 sale gate if B-04 evidence allows.
- **UNVERIFIED data fact:** whether real databases hold open executed BUY positions with `mode IS NULL` (DD-5 rev. 2).
  It is checked by the registration preflight (DD-6 rev. 2), never assumed.
- **Broker evidence** (unchanged): B-08, B-10 and B-31 for the DD-8 values; B-28 for cancel-missing; B-16, B-17, B-27
  and B-30 for error classes; B-29 and B-32 for ids and statuses; B-24 for edge retries.

**Remaining engineering blockers.**

1. User approval of this document, revision 2 (G0).
2. S1: the §4.1 DDL (rev. 2 makes no structural change), the `_m012_*`/`_r012_*` renumbering, and the test pins.
3. The S3 harness before S4, plus the rev. 2 test additions: clock-gap concurrency spies, the EE G_max cases, the
   re-claim bound across ops, the `caps_sell_exposure` conditions, and the settle-timing boundaries.
4. Inside Phase 3, before the engine counts as complete: G3 journal-aware Stage 1; G1/M-6, including the R2-7 changes
   to `instance_lock` and the heartbeat loop (not PK-25 frozen); G2; G4 consumer-driven; the F2 test.

**Remaining live-activation blockers.** These are the ten in §7.2, with the rev. 2 additions:

- the DD-6 preflight includes the NULL-mode count;
- the M-7 G_max sale gate is part of the Phase 6 wiring;
- B-04 stays a LIVE-BLOCKER (`caps_sell_exposure`, the Q_cap = 0 residual, an undetected manual sale, any relaxation
  of the gate);
- the M-6 legacy-path extension now also covers the pre-call re-check.

`LIVE_ENTRIES_ENABLED` stays `False`. **Live trading: NO-GO.**

**Is the document ready for user approval?** **Yes, ready for the user's review and approval decision.** All seven
review items are addressed, and the cross-references in the table above were re-read for agreement (§8 check 10). Two
choices deserve explicit attention when approving:

1. The **stricter Emergency Exit sale gate**. EE may refuse to sell, after at most the grace period, while a protective
   GTT's cancel is unresolved. This is stricter than the legacy ordering. ~~Accept it, or ask for the override above.~~
   **[R3-3] No override (§7.4).**
2. **`caps_sell_exposure`**: the one automated reduction of protection, under `broken` attribution. It is bounded,
   reported, and re-proved at each send.

Approval would allow S1 to begin. It would not authorise registration, live-path changes or live activation.
**[R3]** This readiness statement is carried forward to revision 3 in §7.4.

### 7.4 Revision 3 conclusion

**1. Three corrections completed.**

| Id | Correction | The single rule | Where it is stated |
|---|---|---|---|
| R3-1 | DX classification precedence | Both axes are computed independently (ΔS, ΔP). `none` = increases neither axis. An `attribution_cap` quantity decrease is classified first: `caps_sell_exposure` if all seven conditions hold at that send, otherwise `reduces_protection`. It is never `none`, even when Q_cap equals the open quantity. A cap below the open quantity leaves up to open − Q_cap shares uncovered (position `PROTECTION_NEEDS_REVIEW`); it is not refused when all seven conditions hold. | DX rev. 3 row and decision table; DD-4 (transitions, rev. 3 note); M-1 (reason); M-3 (cond. 1); M-6; §4.1 comment and notes; §4.2 rows 1/5/16 and "never allowed"; §4.4; §5 B-04; A-16; §7.1 |
| R3-2 | Complete G_max | An instrument-wide set 𝒢 of nine sources, deduplicated by broker GTT id. Id-less entries are never merged, different ids are never merged, and an ambiguous id fails closed. G_max = \|𝒢\| (count); S_agg = aggregate worst-case sell quantity, never capped at holdings. SELL only if 𝒢 is established, G_max = 0 and S_agg = 0. Step 3 (which ids EE cancels) is unchanged. **[R4]** The per-member quantity w(m) is refined by R4 (§7.5). | M-7 step 5 (struck), rev. 3 R3-2 row; §1; §4.2 "never allowed"; §5 B-04, B-24; A-14; §7.1 |
| R3-3 | Grace-period semantics | The grace starts at the last relevant cancel sent in the click (no cancel sent → no wait), then one fresh complete R_EE. Only conditions (a)–(e) remove a member (**[R5-2]** (e) restated by R5: every triggered SELL order and SELL leg of the GTT must be proven unable to execute; **[R6]** restated by R6 over the complete triggered-order set T(m), not only its outstanding subset O(m)). UNKNOWN after the grace, ACTIVE in R_EE, an acknowledgement alone, missing, or no usable read → no SELL. One table, G1–G22. No override, and no deferred sale. | M-7 step 4 (struck), rev. 3 R3-3 row and grace-outcome table; §4.2 "never allowed"; §5 B-28, B-31; A-14; §7.2; §7.3 |

**2. Cross-document consistency check.** A sweep of every occurrence of `caps_sell_exposure`, `G_max`, `S_agg`,
`Emergency Exit`, `grace`, `none`, `reduces_protection` and `retry` was read against the three rules (§8.1 check 10):

- The definitions (DX), the examples (DX rev. 3 table, M-7 tests), the transition table (§4.2), the retry table
  (§4.4), the DDL comment (§4.1), the broker register (§5) and the summaries (§1, §7.1–§7.4) agree.
- Superseded rev. 2 text (the DX precedence list, M-7 rev. 2 step 4's wait and step 5's G_max list, the override
  paragraph, §7.2/§7.3 "one product question") is struck and labelled "superseded by R3-n". No active table or
  summary relies on it.
- Unchanged and preserved: R2-2 (cancellation state table), R2-4 (`mode` mapping), R2-5 (`absent_after_settle`
  timing), R2-6 (manual-resolution protection), R2-7 (resume concurrency), the v12 / M-9 design, the 25-decision
  structure and the 33 broker-assumption classifications.

**3. Remaining unresolved design decisions.**

- **None that Phase 3 engineering needs.** The one rev. 2 product question (an EE override) is closed as "no override"
  on the R3 brief's instruction. Reopening it would be a new decision.
- **Revisit items** (not blocking, unchanged): SQLite triggers at registration (DD-5); a `sending → not_sent` pre-call
  abort (M-6 rev. 2 point 8); relaxing the M-7 sale gate if B-04 evidence allows.
- **UNVERIFIED data fact** (unchanged): NULL-mode open positions in real databases (DD-5 rev. 2), checked by the
  registration preflight (DD-6 rev. 2).
- **Broker evidence** (unchanged): B-08, B-10, B-31 (DD-8 values and the grace); B-28 (cancel-missing); B-16, B-17,
  B-27, B-30 (error classes); B-29, B-32 (ids and statuses); B-24 (edge retries). R3 also discloses: two-leg GTT leg
  exclusivity [U], which is not relied on (every SELL leg is summed in S_agg).

**4. Engineering blockers.**

1. User approval of this document, revision 3 (G0).
2. S1: the §4.1 DDL (no structural change in rev. 2 or rev. 3), the `_m012_*`/`_r012_*` renumbering, the test pins.
3. The S3 harness before S4, plus the test additions of R2 and R3: the DX rev. 3 decision table and precedence
   mutations (S4); the nine 𝒢 sources, deduplication, S_agg and the G1–G22 grace outcomes (S7). **[R4]** S7 also
   includes the R4 S_agg cases T1–T3 and their fail-closed cases (§7.5). **[R5]** It also includes T4 and T5 (§7.6). **[R6]** It also includes T6 (§7.7).
4. Inside Phase 3, before the engine counts as complete: G3 journal-aware Stage 1; G1/M-6 with the R2-7 changes; G2;
   G4 consumer-driven; the F2 test.

**5. Live-activation blockers.** The ten in §7.2, unchanged in number, with these notes:

- the DD-6 preflight includes the NULL-mode count (R2-4);
- the Phase 6 `kill_switch` wiring implements the complete M-7 gate: 𝒢, G_max = 0, S_agg = 0, and the grace-outcome
  table (R2-3, R3-2, R3-3);
- B-04 stays a LIVE-BLOCKER (`caps_sell_exposure`, including partial coverage when Q_cap < open, the Q_cap = 0
  residual, and any relaxation of the gate);
- B-08 stays a LIVE-BLOCKER: a GTT the listing does not show cannot be counted in 𝒢 (disclosed residual);
- the M-6 legacy-path extension, including the pre-call re-check.

`LIVE_ENTRIES_ENABLED` stays `False`. **Live trading: NO-GO.**

**6. Is the document ready for final user approval?** **Yes, ready for the user's final review and approval
decision.** The three R3 items are addressed and the cross-references agree (§8.1 check 10). Points that deserve
explicit attention when approving:

1. **A cap below the open quantity** (R3-1, row 2) is allowed automatically when all seven conditions hold. It
   leaves up to open − Q_cap app-recorded shares uncovered, raises an URGENT finding, and puts the position in
   `PROTECTION_NEEDS_REVIEW`. The alternative, refusing it, would reopen R2-1 and D§6.3.
2. **The EE gate is instrument-wide** (R3-2): any SELL GTT on the instrument, including the user's own unlinked
   GTT, blocks the sale until it is confirmed gone. EE does not cancel GTTs it cannot prove are the app's.
3. **No override** (R3-3): after a refusal the user must delete the listed GTTs in Kite, or wait for confirmation,
   and run Emergency Exit again.

Approval would allow S1 to begin. It would not authorise registration of v12, live-path changes, Phase 3 implementation
beyond the approved stages, or live activation.
**[R4]** This readiness statement is carried forward to revision 4 in §7.5.

### 7.5 Revision 4 conclusion

**1. One correction completed.**

| Id | Correction | The single rule | Where it is stated |
|---|---|---|---|
| R4 | S_agg multi-leg worst-case sell exposure | S_agg = the aggregate worst-case quantity of shares that could still be sold, across the entire instrument, by all distinct potentially active GTTs in 𝒢 and their outstanding triggered SELL orders. S_agg = Σ w(m), with w(m) = max over the possible versions of m of the sum of its potentially executable SELL legs, plus the sum of the remaining quantities of its outstanding triggered SELL orders. Legs and orders are never assumed mutually exclusive. An unresolved create counts every SELL leg of its payload. An unresolved modify counts the greater of its old and requested configurations. An unresolved cancel removes nothing until an R3-3 condition holds. Deduplication by broker GTT id is as in R3-2. No cap at holdings. Any ambiguity → UNKNOWN → refuse. The gate is unchanged: 𝒢 established, G_max = 0, S_agg = 0, no override. | M-7 rev. 4 R4 row; M-7 R3-2 row (`w` bullets and deduplication clause struck); M-7 rev. 2 tests row; [R4] record; header; §1; §4.1 notes; §4.2 "never allowed"; §5 B-24; §7.1 (M-7, M-10); §7.2; §7.4 |

**2. Cross-document consistency check.** A sweep of every occurrence of `S_agg`, `G_max`, `requested_quantity`,
`w(m)`, `w =`, `leg`, "maximum over", `unfilled`, "acknowledg" and "cap" was read against the R4 rule (§8.4 check 9):

- The active definition (M-7 rev. 4 row), the R3-2 membership and deduplication rules, the R3-3 removal conditions,
  the §4.2 "never allowed" list, the B-24 posture and the summaries (§1, §7.1, §7.2, §7.4, §7.5) agree.
- Superseded rev. 3 text (the R3-2 known-id, id-less create and triggered-member `w` bullets; "the maximum over that
  evidence"; the B-24 "its requested quantity") is struck and labelled "superseded by R4". No active table or summary
  relies on it.
- R2-3 and R3-2 text that names a single quantity ("S_agg uses the larger quantity", "open − S_agg", "w") is either
  qualified with an R4 pointer or consistent with R4 as written.
- Unchanged and preserved: R3-1, R3-3 (grace start, conditions (a)–(e), G1–G22, no override), the R3-2 membership set
  and deduplication, the sale gate, EE cancellation scope (step 3), R2-2, R2-4…R2-7, the DDL, the v12 / M-9 design,
  the §4.2 transition rows, the §4.4 retry rules, the 25-decision structure and the 33 broker-assumption
  classifications.

**3. Remaining unresolved design decisions.** None that Phase 3 engineering needs. Revisit items, the UNVERIFIED
data fact and the broker evidence are as in §7.4 item 3. Two-leg OCO exclusivity [U] is still not relied on, and R4
makes that explicit for every version and every triggered order.

**4. Engineering blockers.** As in §7.4 item 4, with: user approval of this document, revision 4 (G0); the S7 tests
include the R4 cases T1 (two SELL legs both counted), T2 (unresolved multi-leg modify: the greater of old and
requested), T3 (several outstanding triggered SELL orders summed by remaining quantity), and their fail-closed
cases. No new blocker, no structural change.

**5. Live-activation blockers.** The ten in §7.2, unchanged in number and content. `LIVE_ENTRIES_ENABLED` stays
`False`. **Live trading: NO-GO.**

**6. Is the document ready for final user approval?** **Yes, ready for the user's review and approval decision.**
R4 is addressed and the cross-references agree (§8.4 check 9). R4 changes no point listed in §7.4 item 6. Approval
would allow S1 to begin. It would not authorise registration of v12, live-path changes, Phase 3 implementation
beyond the approved stages, or live activation.
**[R5]** This readiness statement is carried forward to revision 5 in §7.6.

### 7.6 Revision 5 conclusion

**1. Two clarifications completed.**

| Id | Correction | The single rule | Where it is stated |
|---|---|---|---|
| R5-1 | Id-less CREATE and B-24 multiplicity | A known request payload quantity is what one GTT from that request could sell. It never establishes the complete set of resulting GTTs. An id-less create whose resulting GTTs' number or identity cannot be established has w UNKNOWN, so S_agg = UNKNOWN. The payload quantity is never presented as a worst case or a bound. Each independently identified resulting GTT is a separate member, deduplicated by broker GTT id, with its own w under R4. One recorded id never proves a single GTT; siblings are caught by source 6 or the ambiguous-id rule. Id-less entries are never merged. Any ambiguity fails closed. An unresolved id-less create never permits a sale (G21). | M-7 Rev. 5 R5-1 row; M-7 R4 row case 2 (lower-bound sentence struck); M-7 R3-2 row (deduplication and UNKNOWN pointers); G21; [R5-1] record; R3-2 and R4 records; header; §1; §4.1 notes; §4.2 "never allowed"; §5 intro and B-24; §7.1 (M-7, M-10); §7.2 |
| R5-2 | Condition (e) for triggered multi-leg GTTs | A triggered member leaves 𝒢 only when complete, unambiguous evidence shows: every ~~outstanding~~ **[R6] (superseded by R6: every order in the complete triggered-order set T(m), with T(m) established as complete)** triggered SELL order of the GTT terminal; each with zero remaining unfilled quantity; every other SELL leg proven unable to execute; every fill recorded and reconciled against the relevant position; and evidence covering the whole GTT. Otherwise the member stays, its known remaining exposure stays in S_agg (UNKNOWN if not established), and Emergency Exit refuses. One terminal order never removes a multi-leg GTT. G6 refuses whenever complete (e) evidence is unavailable. | M-7 R3-3 row (condition (e) struck and restated; "never sufficient" list); G6 (and the G20 pointer); M-7 Rev. 5 R5-2 row; M-7 R3-2 row (source 8 pointer); M-7 R4 row (leg term and case 5 pointers); [R5-2] record; R3-2 and R4 records; header; §1; §4.2 "never allowed"; §7.1 (M-7); §7.2; §7.4 |

**2. Cross-document consistency check.** A sweep of every occurrence of `S_agg`, `G_max`, `B-24`, `id-less`,
`request_payload`, "lower bound", `(e)`, `G6`, `triggered`, "SELL leg", `terminal`, `remaining` and `Emergency Exit`
was read against R5-1 and R5-2 (§8.5 check 9):

- The active rules (M-7 R3-2, R4, R3-3, the Rev. 5 R5-1 and R5-2 rows, the grace-outcome table G6, G20 and G21), the
  §4.2 "never allowed" list, the B-24 posture, the future-test requirements (R4, R5-1 and R5-2 records) and the
  summaries (§1, §7.1, §7.2, §7.4, §7.6) agree.
- Superseded text is struck and labelled "superseded by R5": the R4 case 2 "lower bound" sentence, the B-24 "as a lower
  bound", the R3-2 and R4 record "lower bound" clauses, the R4 record's "id-less" test qualifier, the rev. 3 condition
  (e) and the old G6 member cell. No active table or summary relies on struck text.
- The R3-2 and R4 records' statement that leg exclusivity "affects only the reported quantity" is qualified with an R5-2
  note: exclusivity is still not relied on, so it can only keep a member in 𝒢.
- Unchanged and preserved: DX and its seven conditions (R3-1); the R3-2 membership set and deduplication; the R4
  formula, alternative old/requested MODIFY versions and the payload rule for creates with an id; the R2-2 cancellation
  table and the acknowledgement rules (G4, G5); the grace start at T_last and the fresh R_EE; conditions (a)–(d); the
  G1–G22 numbering; no override and no deferred sale; the sale gate; the DDL; the v12 / M-9 design; the §4.2 transition
  rows; the §4.4 retry rules; the 25-decision structure; and the 33 broker-assumption classifications.

**3. Remaining unresolved design decisions.** None that Phase 3 engineering needs. Revisit items, the UNVERIFIED
data fact and the broker evidence are as in §7.4 item 3. R5 discloses three points of interpretation, all fail-closed:

- **Completeness of the resulting-GTT set (R5-1).** It is bounded by listing completeness, like all of 𝒢. A resulting
  GTT that no complete read lists is the existing B-08 residual. When the op records a `result_gtt_id`, its entry
  becomes that id's member (R3-2, R3-3 (d), unchanged), and any sibling is counted through source 6.
- **"Zero remaining unfilled quantity" (R5-2 item 2; [R6] item 3 of condition (e) rev. 6).** It means the read shows nothing left that can still fill
  (R3's "zero pending quantity"), with any unfilled quantity reported by the broker as cancelled or rejected. It does
  not mean "filled = order quantity". That reading would never release a triggered order the broker rejected (B-05).
- **Practical effect of R5-2.** A two-leg GTT that triggered on one leg keeps Emergency Exit refusing for the
  instrument until the other leg is proven unable to execute, or the GTT is explicitly `deleted`/`cancelled`. Relaxing
  this needs Phase 9 evidence of Kite's OCO behaviour, and would be a new decision. No §5 entry is added.

**4. Engineering blockers.** As in §7.5 item 4, with: user approval of this document, revision 5 (G0); the S7 tests
include T4 (id-less CREATE multiplicity → S_agg UNKNOWN, with the identified-GTTs positive control) and T5 (partially
resolved multi-leg triggered GTT → member retained, remaining exposure counted, refused; UNKNOWN variant; (e) control).
No new blocker, no structural change.

**5. Live-activation blockers.** The ten in §7.2, unchanged in number and content. `LIVE_ENTRIES_ENABLED` stays
`False`. **Live trading: NO-GO.**

**6. Is the document ready for final user approval?** **Yes, ready for the user's review and approval decision.**
R5-1 and R5-2 are addressed and the cross-references agree (§8.5 check 9). R5 changes no point listed in §7.4 item 6.
Approval would allow S1 to begin. It would not authorise registration of v12, live-path changes, Phase 3
implementation beyond the approved stages, or live activation.
**[R6]** This readiness statement is carried forward to revision 6 in §7.7.

### 7.7 Revision 6 conclusion

**1. One correction completed.**

| Id | Correction | The single rule | Where it is stated |
|---|---|---|---|
| R6 | Complete triggered-order set T(m) vs outstanding subset O(m) | **T(m)** = every triggered SELL order associated with GTT member m: open, partially filled, terminal, cancelled or rejected, plus every order the evidence on the GTT's trigger and legs indicates but which is not yet visible. It is a historical and current completeness concept. **O(m)** ⊆ T(m) = the orders that could still execute (R4, unchanged). Condition (e) rev. 6 removes a triggered member only when T(m) is complete, every order in it is terminal with zero remaining unfilled quantity (unfilled quantity explicitly reported cancelled or rejected), every SELL leg is proven unable to execute, every fill is reconciled, and the evidence covers the entire GTT. An expected but not-yet-visible order makes the evidence incomplete. An empty O(m) or visible terminal orders never prove completeness. Otherwise the member stays, its outstanding exposure stays in S_agg under the unchanged R4 formula (UNKNOWN if exposure or completeness cannot be established), and Emergency Exit refuses. O(m) feeds only Σ r(o); a proven terminal order contributes 0. | M-7 R3-3 row (rev. 5 condition (e) struck, rev. 6 stated; "never sufficient" list); G6 (and the G20 pointer); M-7 Rev. 6 R6 row; M-7 R4 row (`O(m)` term, case 5); M-7 R3-2 row (source 8); M-7 Rev. 5 R5-2 row; [R6] record and [R5-2] record pointer; header; §1; §4.1 notes; §4.2 "never allowed"; §5 intro; §7.1 (M-7, M-10); §7.2; §7.4; §7.6 |

**2. Cross-document consistency check.** A sweep of every occurrence of `O(m)`, `T(m)`, "outstanding", `(e)`, `G6`,
`terminal`, "not yet visible", "expected order", "zero remaining", "every triggered SELL order", "its order" and
`B-13` was read against R6 (§8.6 check 9):

- The active rules (M-7 R3-2 source 8, the R4 terms and case 5, condition (e) rev. 6 in the R3-3 row, the Rev. 5 R5-2
  and Rev. 6 R6 rows, G6 and G20), the §4.2 "never allowed" list, the future-test requirements (R5-2 and R6 records)
  and the summaries (§1, §7.1, §7.2, §7.4, §7.6, §7.7) use **T(m)** for completeness and **O(m)** for outstanding
  exposure.
- Superseded text is struck and labelled "superseded by R6": the rev. 5 condition (e) (all five items and its
  fallback sentence), the rev. 5 G6 member and outcome cells, and the word "outstanding" in the rev. 5 item (1) of the
  [R5-2] record and of the §7.6 R5-2 row, and "rev. 5" in the R3-2 source 8 and R4 case 5 removal pointers (now
  rev. 6). No active table or summary relies on struck text.
- The remaining uses of "outstanding" refer to O(m) in its R4 meaning (exposure), or to outstanding protection ops,
  and are consistent as written.
- Unchanged and preserved: DX and its seven conditions; the R3-2 membership set and deduplication; the R4 formula, L(v),
  r(o), the B-13 full-leg rule and old/requested MODIFY version handling; R5-1 and B-24; the R5-2 safeguards; the R3-3
  grace start, the fresh R_EE, conditions (a)–(d), C and D, and the cancellation rules; the G1–G22 numbering and every
  refusal outcome; the sale gate; no override and no deferred sale; the DDL; the v12 / M-9 design; the §4.2 transition
  rows; the §4.4 retry rules; the 25-decision structure; and the 33 broker-assumption classifications.

**3. Remaining unresolved design decisions.** None that Phase 3 engineering needs. Revisit items, the UNVERIFIED
data fact and the broker evidence are as in §7.4 item 3. R6 discloses one point of interpretation, fail-closed:

- **What makes an order "expected" (R6 item 1).** R6 does not assume how Kite reports a triggered leg, or how many
  orders one triggered leg produces (B-13, LIVE-P9). T(m) counts every order the available evidence indicates. Where
  the evidence cannot establish which orders should exist, T(m) is not complete and the member stays in 𝒢. Phase 9
  evidence on B-13 may make (e) reachable in more cases; it would never permit a sale on an incomplete T(m).

**4. Engineering blockers.** As in §7.6 item 4, with: user approval of this document, revision 6 (G0); the S7 tests
include T6 (expected triggered order not yet visible → T(m) not complete, member retained, S_agg UNKNOWN when the
missing order's exposure cannot be established, refused). No new blocker, no structural change.

**5. Live-activation blockers.** The ten in §7.2, unchanged in number and content. `LIVE_ENTRIES_ENABLED` stays
`False`. **Live trading: NO-GO.**

**6. Is the document ready for final user approval?** **Yes, ready for the user's review and approval decision.**
R6 is addressed and the cross-references agree (§8.6 check 9). R6 changes no point listed in §7.4 item 6. Approval
would allow S1 to begin. It would not authorise registration of v12, live-path changes, Phase 3 implementation beyond
the approved stages, or live activation.

---

## 8. Final integrity checks

### 8.6 Revision 6 checks

Listed first, and numbered 8.6 so that existing references to §8.1–§8.5 stay valid. Run after the revision 6 content
edits were complete; recording this section changes only this document. All checks are read-only. Git ran with
`--no-optional-locks` where it reads the worktree.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged; `git log -1` is still HEAD (no commit) [V] |
| 2 | Worktree inventory | `diff status_before.txt status_after.txt`: **identical**, 101 entries; this document was already `??` [V] |
| 3 | Pre-existing content | `sha256sum -c hashes_before.txt`: **100 / 100** other entries byte-identical. Only this document changed. No source, schema, migration registration, test, configuration or other doc was modified [V] |
| 4 | Staged / committed / pushed / stashed | `git diff --cached`: 0 files; `git stash list`: empty; no commit, push, merge, reset, clean, checkout or branch switch [V] |
| 5 | `backend/execution.py` | identical to HEAD: `git hash-object` = `HEAD:backend/execution.py` blob (`fc8e1ba0…`), `git diff --quiet HEAD -- backend/execution.py` clean, and the working-file sha256 (`76396481…`) is the same at the start and at the end [V] |
| 6 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`: 11 production + 2 model files), at the start and at the end, with a new read-only scratchpad `pk25.py` that imports nothing from `backend` [V] |
| 7 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, read by grep, not edited) [V] |
| 8 | Document structure | Compared with the rev. 5 copy by scratchpad `structure.py`: 25 decisions (DX + 24 decision `###` headings); the only added headings are §7.7 and §8.6, and none was removed. No table has an inconsistent column count (code spans and escaped pipes excluded). Every line has balanced strikethrough markers. B-01…B-33 are present with **every Blocks value identical** to rev. 5. The §4.1 DDL code block, the §4.2 transition rows and the §4.4 retry table are **identical** to rev. 5. The grace-outcome table still has 22 rows, G1–G22 in order; only G6 (the "removed" and "sale outcome" cells) and G20 (the R_EE status pointer) changed, and every refusal outcome is kept. 16 rev. 5 lines were edited in place, every one an intended R6 change: the [R5-2] record corrected-rule cell; the M-7 R3-2, R4, R3-3 and Rev. 5 R5-2 rows; G6 and G20; the §7.1 header and the M-7/M-10 rows; §7.2; the §7.4 R3-3 row and item 4; the §7.6 R5-2 row and item 3 bullet; the closing line. Line endings stay LF [V] |
| 9 | Cross-reference consistency | Every occurrence of `O(m)`, `T(m)`, "outstanding", `(e)`, "(e) rev. 5", `G6`, `terminal`, "not yet visible", "expected order", "zero remaining", "every triggered SELL order", "its order" and `B-13` was read against R6. Completeness is evaluated on T(m) in condition (e) rev. 6, G6, R3-2 source 8, the R4 terms and case 5, the R5-2 and R6 rows, §4.2 and the summaries. O(m) is used only for the R4 exposure term. The rev. 5 condition (e), the rev. 5 G6 cells, "outstanding" in the rev. 5 item (1) ([R5-2] record, §7.6) and the two "rev. 5" removal pointers are struck and labelled "superseded by R6". The remaining "outstanding" uses refer to O(m) in its R4 meaning or to outstanding protection ops. Struck rev. 2–5 text is otherwise unchanged. §8.1–§8.5 are kept as historical records [V] |
| 10 | Forbidden actions | none: no Kite MCP tool, broker API, HTTP request, application start, test run, migration, module import from `backend`, or access to `.env`, credentials, the account database, its backups or protected account files (`backend/ui_prefs.json` included) [V: session command log] |
| 11 | Scratch files | `status_before.txt`, `status_after.txt`, `hashes_before.txt`, `PHASE_3_DESIGN_DECISIONS.rev5.md`, `pk25.py` and `structure.py` are in the session scratchpad, outside the repository [V] |

### 8.5 Revision 5 checks

Listed first, and numbered 8.5 so that existing references to §8.1–§8.4 stay valid. Run after the revision 5 content
edits were complete; recording this section changes only this document. All checks are read-only. Git ran with
`--no-optional-locks` where it reads the worktree.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged; `git log -1` is still HEAD (no commit) [V] |
| 2 | Worktree inventory | `diff status_before.txt status_after.txt`: **identical**, 101 entries; this document was already `??` [V] |
| 3 | Pre-existing content | `sha256sum -c hashes_before.txt`: **100 / 100** other entries byte-identical. Only this document changed. No source, schema, migration registration, test, configuration or other doc was modified [V] |
| 4 | Staged / committed / pushed / stashed | `git diff --cached`: 0 files; `git stash list`: empty; no commit, push, merge, reset, clean, checkout or branch switch [V] |
| 5 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) [V] |
| 6 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`: production + model files), at the start and at the end, with a new read-only scratchpad `pk25.py` that imports nothing from `backend` [V] |
| 7 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, read by grep, not edited) [V] |
| 8 | Document structure | Compared with the rev. 4 copy by scratchpad `structure.py`: 25 decisions (DX + 24 decision `###` headings); the only added headings are §7.6 and §8.5, and none was removed. No table has an inconsistent column count (code spans and escaped pipes excluded). Every line has balanced strikethrough markers (49 spans). B-01…B-33 are present with **every Blocks value identical** to rev. 4. The §4.1 DDL code block, the §4.2 transition rows and the §4.4 retry table are **identical** to rev. 4. The grace-outcome table still has 22 rows, G1–G22 in order. 17 rev. 4 lines were edited in place, every one an intended R5 change: R3-2 record remaining-unknown cell; R4 record tests and remaining-unknown cells; M-7 R3-2, R4 and R3-3 rows; G6, G20 and G21; B-24; the §7.1 header and the M-7/M-10 rows; §7.2; the §7.4 R3-3 row and item 4; the closing line. Line endings stay LF [V] |
| 9 | Cross-reference consistency | Every occurrence of `S_agg`, `G_max`, `B-24`, `id-less`, `request_payload`, "lower bound", `(e)`, `G6`, `triggered`, "SELL leg", `terminal`, `remaining`, `unfilled`, "its order" and `Emergency Exit` was read against R5-1 and R5-2. Every "lower bound" is struck and labelled "superseded by R5", or is part of the R5 explanation. The rev. 3 condition (e) and the old G6 member cell are struck and restated. The R4 record and R4 case 4 references to "an R3-3 condition (a)–(e)" are consistent with the restated (e). The R3-2/R4 records' "affects only the reported quantity" is qualified with an R5-2 note. Struck rev. 2–4 text is otherwise unchanged. §8.1–§8.4 are kept as historical records [V] |
| 10 | Forbidden actions | none: no Kite MCP tool, broker API, HTTP request, application start, test run, migration, module import from `backend`, or access to `.env`, credentials, the account database, its backups or protected account files (`backend/ui_prefs.json` included) [V: session command log] |
| 11 | Scratch files | `status_before.txt`, `status_after.txt`, `hashes_before.txt`, `PHASE_3_DESIGN_DECISIONS.rev4.md`, `pk25.py` and `structure.py` are in the session scratchpad, outside the repository [V] |

### 8.4 Revision 4 checks

Listed first, and numbered 8.4 so that existing references to §8.1–§8.3 stay valid. Run after the revision 4 edits
were complete. All checks are read-only. Git ran with `--no-optional-locks` where it reads the worktree.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged; `git log -1` is still HEAD (no commit) [V] |
| 2 | Worktree inventory | `diff status_before.txt status_after.txt`: **identical**, 101 entries; this document was already `??` [V] |
| 3 | Pre-existing content | `sha256sum -c hashes_before.txt`: **100 / 100** other entries byte-identical. Only this document changed. No source, schema, migration registration, test, configuration or other doc was modified [V] |
| 4 | Staged / committed / pushed / stashed | `git diff --cached`: 0 files; `git stash list`: empty; no commit, push, merge, reset, clean, checkout or branch switch [V] |
| 5 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) [V] |
| 6 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`: production + model files), at the start and at the end, with a new read-only scratchpad `pk25.py` that imports nothing from `backend` [V] |
| 7 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, read by grep, not edited) [V] |
| 8 | Document structure | Compared with the rev. 3 copy by scratchpad `structure.py`: 25 decisions (DX + 24 decision `###` headings), headings identical. No table has an inconsistent column count (code spans and escaped pipes excluded). Every line has balanced strikethrough markers. B-01…B-33 present with **every Blocks value identical** to rev. 3. The §4.1 DDL code block, the §4.2 transition rows and the §4.4 retry table are **identical** to rev. 3. 14 rev. 3 lines were edited in place, every one an intended R4 change (R3-2 record pointer, §1 bullet, M-7 rev. 2 tests row, M-7 R3-2 row, G6 cell, §4.2 last bullet's punctuation, B-24, §7.1 header and the M-7/M-10 rows, §7.2, §7.4 table and item 4, closing line). Line endings stay LF [V] |
| 9 | Cross-reference consistency | Every occurrence of `S_agg`, `G_max`, `requested_quantity`, "requested quantity", `w(m)`, `w =`, `leg`, "maximum over", `unfilled`, "larger quantity" and `open − S_agg` was read against R4. Contradicting rev. 3 text is struck and labelled "superseded by R4": the three R3-2 `w` bullets, "the maximum over that evidence", and the B-24 "its requested quantity". Single-quantity wording is qualified with an R4 pointer: the R2-3 "S_agg uses the larger quantity" and the G6 "w = unfilled order quantity". `open − S_agg` (partial sale not selected) and the R3-2 record and tests are consistent as written. Struck rev. 2 text is unchanged [V] |
| 10 | Forbidden actions | none: no Kite MCP tool, broker API, HTTP request, application start, test run, migration, module import from `backend`, or access to `.env`, credentials, the account database, its backups or protected account files (`backend/ui_prefs.json` included) [V: session command log] |
| 11 | Scratch files | `status_before.txt`, `status_after.txt`, `hashes_before.txt`, `PHASE_3_DESIGN_DECISIONS.rev3.md`, `pk25.py` and `structure.py` are in the session scratchpad, outside the repository [V] |

### 8.1 Revision 3 checks

Run after the revision 3 edits were complete. All checks are read-only. Git ran with `--no-optional-locks` where it
reads the worktree.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged; `git log -1` is still HEAD (no commit) [V] |
| 2 | Worktree inventory | `diff status_before.txt status_after.txt`: **identical**, 101 entries. No entry removed, added or changed in status; this document was already `??` [V] |
| 3 | Pre-existing content | `sha256sum -c hashes_before.txt`: **100 / 100** other entries byte-identical. Only this document changed. No source, schema, migration registration, test, configuration or other doc was modified [V] |
| 4 | Staged / committed / pushed / stashed | `git diff --cached`: 0 files; `git stash list`: empty; no commit, push, reset, clean, checkout or branch switch [V] |
| 5 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) [V] |
| 6 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`: 11 production + 2 model files), at the start and at the end, with a new read-only scratchpad `pk25.py` that imports nothing from `backend` [V] |
| 7 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, read by grep, not edited) [V] |
| 8 | Document structure | 25 decisions (DX + 24 decision `###` headings), unchanged. No table has an inconsistent column count (code spans and escaped pipes excluded). Every line has balanced strikethrough markers (35 spans). R3-1, R3-2 and R3-3 tags present. The §5 register still has B-01…B-33, with **every Blocks value identical** to rev. 2 (compared against the rev. 2 copy). Line endings stay LF, as in rev. 2 [V: scratchpad `structure.py`] |
| 9 | New citations | D:648-686, D:655, D:660, `db.py:2540-2552`, `routes/kill_switch.py:91-140, 200-216, 273-283, 555-562` were read in this session [V] |
| 10 | Cross-reference consistency | Every occurrence of `caps_sell_exposure`, `G_max`, `S_agg`, `Emergency Exit`, `grace`, `none`, `reduces_protection`, `retry` and "override" was read against R3-1…R3-3. Contradicting rev. 2 text is struck and labelled "superseded by R3-n" (DX precedence, M-7 rev. 2 step 4 wait, step 5 G_max list, the override paragraph, §7.2/§7.3 product question), or qualified with an R3 pointer (`open − G_max`, "at most the grace period", the R2-1 added test, the R2-3 added tests). Revision-history rows R2-1…R2-7 are kept as records [V] |
| 11 | Forbidden actions | none: no Kite MCP tool, broker API, HTTP request, application start, test run, migration, module import from `backend`, or access to `.env`, credentials, the account database, its backups or protected account files [V: session command log] |
| 12 | Scratch files | `status_before.txt`, `status_after.txt`, `hashes_before.txt`, `PHASE_3_DESIGN_DECISIONS.rev2.md`, `pk25.py` and `structure.py` are in the session scratchpad, outside the repository [V] |

### 8.2 Revision 2 checks (as recorded at revision 2)

Run after the revision 2 edits were complete. All checks are read-only. Git ran with `--no-optional-locks`.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged; `git log -1` is still HEAD (no commit) [V] |
| 2 | Worktree inventory | `diff status_before.txt status_after.txt`: **identical**, 101 entries. This document was already untracked (`??`) at the start, so revision 2 adds no entry [V] |
| 3 | Pre-existing content | `sha256sum -c hashes_before.txt`: **100 / 100** other entries byte-identical. Only this document changed. No source, schema, migration, test, configuration or other doc was modified [V] |
| 4 | Staged / committed / stashed | `git diff --cached`: 0 files; `git stash list`: empty; no commit, push, reset, clean, checkout or branch switch [V] |
| 5 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) [V] |
| 6 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`: 11 production + 2 model files), at the start and at the end [V] |
| 7 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, read by grep, not edited) [V] |
| 8 | Document structure | 25 decisions (DX + 24 `###` decision headings), the same as rev. 1. No table row has an inconsistent column count. Strikethrough spans balanced (16). R2 tags present for R2-1…R2-7. 59 rev. 1 lines were edited in place, every one an intended R2 amendment: 25 of them are the §7.1 rows that gained the "Revision 2" column, and one is the closing line [V: scratchpad `structure.py`] |
| 9 | New citations | Every file:line cited for the first time in rev. 2 was re-checked by `grep -n` against the current files, and corrected where off by a line or two [V] |
| 10 | Cross-reference consistency | A grep sweep for rev. 1 phrases that would contradict R2-1…R2-7 ("retried automatically", "once flat", "exclusive-only", "relies on B-04", "one automatic `reduces_protection`", "`reset_cycle_memory()`"). Every hit is now struck through, or qualified with an R2 pointer [V] |
| 11 | Forbidden actions | none: no Kite MCP tool, broker API, HTTP request, application start, test run, migration, module import from `backend`, or access to `.env`, credentials, the account database, its backups or protected account files [V: session command log] |
| 12 | Scratch files | `status_before.txt`, `status_after.txt`, `hashes_before.txt`, `PHASE_3_DESIGN_DECISIONS.orig.md` and the helper scripts are in the session scratchpad, outside the repository [V] |

### 8.3 Revision 1 checks (as recorded at revision 1)

Run after sections 0–7 were written. All checks are read-only. Git ran with `--no-optional-locks`.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged [V] |
| 2 | Worktree inventory | `diff status_before.txt status_after.txt` shows exactly one added line, `?? docs/PHASE_3_DESIGN_DECISIONS.md`; the other 100 entries are unchanged [V] |
| 3 | Pre-existing content | `git hash-object` of all 100 pre-existing entries is identical before and after [V]. No source, schema, migration, test or other doc was modified |
| 4 | New files | only this document. Scratch files (`status_*.txt`, `hashes_before.txt`, `pk25.py`) are in the session scratchpad, outside the repository [V] |
| 5 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) [V] |
| 6 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`), same as at the start [V] |
| 7 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, read by grep, not edited) [V] |
| 8 | Staged / committed / stashed | nothing staged, no commit, no push, `git stash list` empty; no reset, clean, checkout or branch switch [V] |
| 9 | Broker / network / app / tests / database | none: no Kite MCP tool, broker API, HTTP request, application start, test run, migration, or access to `.env`, the account database, its backups or `ui_prefs.json` [V: session command log] |

**Phase 3 has not been started.** S1 and every later stage wait for explicit approval of this document (revision 6).
