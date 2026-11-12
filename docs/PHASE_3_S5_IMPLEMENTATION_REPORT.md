# Phase 3 Stage S5: implementation report (operation safety, fencing and ambiguous responses)

**Date:** 2026-11-11 · **Branch / HEAD:** unchanged (`35a0e07f…`); nothing staged, committed, pushed or stashed ·
**Specification:** R6 rows 5–8, 16, 17c, 26, 27; M-3, M-4; DD-10 G2; R2-7 points 4, 6, 8

**Result: S5 accepted for the engine path.** The two S3 legacy residuals are **not** closed (§5), by design and
consistent with R6. Dark only; FakeKite and tmp databases only.

## 1. Files

| File | Status | Purpose |
|---|---|---|
| `backend/protection_send.py` | new | Two-transaction send; exception classes; M-4 association; G2; M-3 retry |
| `backend/protection_runtime.py` | new | `send_gate` (DD-8, M-6 flag, clock gap) and `pre_call_gate` |
| `backend/instance_lock.py` | modified (Edit, surgical) | M-6/R2-7 primitives: shared baseline `_detect_gap_locked`, `clock_gap_check`, `resume_seq`, `take_unnoted_resume`, `consume_resume`; `heartbeat_once` uses the shared detector; `run_heartbeat_loop` consumes any unnoted `resume_seq` |
| `backend/protection.py` | modified (own S4 file) | `resolve_superseded_result` (§4.3, used by late responses) |
| `tests/test_protection_s5.py` | new | 36 tests |

## 2. The fenced send: where authorisation is established

| Step | Transaction | What is checked under the SQLite write lock |
|---|---|---|
| A. write-ahead | `journal_transaction` (BEGIN IMMEDIATE + epoch) | op state (`intent`, or `unknown` for row 16); overlay; exit in flight; **DX against this send's read**; clock gap (M-6); DD-8 freshness; shared `RESUMED_AFTER_SUSPEND`; DD-9; M-3 for retries; target listed. Then `→ sending`, `attempts+1`, `sent_at`, the target's pre-state recorded in `evidence`. COMMIT. |
| B. the call | `journal_transaction` **held across the broker call** | epoch (by the fence); op still `sending`, same `op_uuid`, same attempt; epoch equals the context's; `pre_call_gate` (authority + clock gap); broker chosen from the op's own `mode` (`check_mode`); the call; the response recorded by the M-4 CAS. COMMIT. |

**Investigation result (mandate §6).** Authorisation of the broker write *can* be made atomic with respect to
ownership changes: a takeover must write `app_instance`, which needs the write lock that transaction B holds for
the whole call. A successor's takeover attempted during a send fails with `database is locked` (T16) and succeeds
afterwards; the stale process's next journal write is fenced out. This is stricter than R2-7 point 8 (which ran the
pre-call check outside any transaction) and weakens no R6 rule.

**Exact residual window.** None between the authority check and the request leaving the process. What remains is
outside the process: a request authorised at send time may be *processed* by the broker after the client timed out
(B-31). That is the ambiguous outcome handled by M-3/M-4 (`unknown`, never re-sent for create/up/loosen, resolved by
reads). Accepted costs: other writers wait up to one broker call (bounded by the 7 s SDK timeout, under the 10 s
busy timeout); a frozen process holding B blocks a takeover until it resumes (fail-closed, as Phase 2 MP-S4).

## 3. Outcomes and association

| Case | Behaviour | Test |
|---|---|---|
| Accepted | `sending → acknowledged`; create links the id with the DD-6 CAS (linked elsewhere → `needs_review`) | T01 |
| Definite refusal (create/modify: Order/Input/Permission; delete: also Token) | `rejected` | T02, T05 |
| Ambiguous (5xx, transport, Token on create, malformed id) | `unknown`, settle count reset; never re-sent | T03, T04 |
| Refused inside the process (`InstanceNotOwner`, mode error) or a pre-call gate | no call, no transition (`sending` stays; recovery → `unknown`) | T15, T17 |
| Crash after the broker effect | `sending` stays (write-ahead), exactly one GTT at the broker | T30 |
| Late response, same attempt, op `unknown` | row 17c `acknowledged`/`rejected` | T20, T21 |
| Response for a non-current attempt; duplicate; reordered | evidence only | T22, T23 |
| Terminal, consistent / contradictory | evidence only / URGENT finding, position `PROTECTION_NEEDS_REVIEW`, op unchanged | T24, T25 |
| Response that cannot be committed (authority lost at commit) | G2 CRITICAL log line, no database write | T18, T19 |
| Superseded op answered | the §4.3 action in the same transaction | T07 |

## 4. Fencing and takeover tests (in-process; the multi-process versions are in S9)

T13 old owner's claim after a committed takeover → `FencingError`, role `lost`, no broker call · T14 takeover between
claim and send → no call, op `intent` · T15 takeover between the `sending` commit and the call → no call, op
`sending` · T16 takeover attempted during the call → refused by the held lock · T32 op moved by recovery between A and
B → no call.

## 5. The two S3 residuals (`test_residual_MP_E2`, `test_residual_MP_E3`)

Both pin **legacy** paths: an unfenced write by a pre-existing loop (`mp_markers` "legacy" write) and the legacy
`broker_write` fence, which checks the in-memory role only. They are **closed for the engine** (every journal write is
fenced, T21 of S4; every engine broker write is inside a held fenced transaction, T13–T16) and **not closed for the
legacy code**:

- R6 M-6 (c) and RR §14 item 5 make any change of legacy-path behaviour a separate authorisation and a live-activation
  blocker. Closing them means changing every legacy writer or `instance_lock.broker_write`, which every live order
  path (including the frozen `execution.py` through the SDK fence) uses.
- The test files were therefore **not** changed: the mandate allows updating them only when the gap is genuinely
  closed, and for the legacy code it is not.
- Ready-to-authorise proposals are in the final report (§J, BLOCKER items B-L1/B-L2): (1) `broker_write` re-reads
  `app_instance` before admitting a write (narrows the legacy window to milliseconds); (2) a DB-level fence (a TEMP
  trigger per connection refusing writes when the epoch is not the connection owner's), which closes E2 atomically.

## 6. Tests and mutations

`tests/test_protection_s5.py`: **36 passed**. Neighbour suites after the `instance_lock` change
(`test_protection_s4.py test_instance_lock.py test_startup_reconciliation.py test_broker_write_fence.py
test_cli_instance_lock.py`): all passed, 3 skipped (the WIN-INT placeholders), guard `unexpected 0`.

| # | Mutation | Result |
|---|---|---|
| M1 | the call made outside the held fenced transaction | caught (T15) |
| M2 | M-4 CAS drops the attempt | caught (T22) |
| M3 | no re-read of the op before the call | caught (T32) |
| M4 | retry without a read-back of the target | caught (T27) |
| M5 | pre-call gate removed | caught (T17) |
| M6 | an ambiguous create treated as a definite refusal | caught (T03) |
| M7 | DD-8 freshness not checked | caught (T09) |
| M8 | shared resume flag not consulted | caught (T12) |
| M9 | **production** `fenced_transaction` epoch check removed (restored, sha256 verified) | caught (T13) |
| M10 | automatic op sent whatever its class | caught (T11) |
| M11 | a create retried | caught (T03) |
| M12 | G2 path writes the database | caught (T18) |

## 7. Acceptance

All S5 criteria met for the engine path (stale-owner DB and broker writes refused; ownership change at every boundary
tested; ambiguous outcomes never re-sent; late, duplicate and reordered responses associated by `(op_id, op_uuid,
attempt, state)`; G2 logs without writing; all mutations caught). Legacy residuals carried forward as BLOCKERs.

**Broker behaviour that remains unverified:** B-26 (5xx semantics), B-30 (Token on delete), B-31 (processing after
timeout), B-16 (rate-limit error class). The classification is fail-closed for each.
