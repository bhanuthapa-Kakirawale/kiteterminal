# Phase 3 Stage S8: implementation report (runtime continuity, clocks and recovery)

**Date:** 2026-11-11 · **Branch / HEAD:** unchanged · **Specification:** M-6 and R2-7 (points 1–8), DD-8 rule iii,
DD-9, DD-10 G1/G4 (R2-7), G3 journal-aware Stage 1, rows 3, 10 and 25

**Result: S8 accepted for the offline scope.** The M-6 (c) legacy-path extension (detection in `loop_may_run`,
`broker_write`, `confirm_time_blockers`, refusal of `sync_protection` resize-up) is **not** done: R6 makes it a separate
authorisation and a live-activation blocker.

## 1. Files

| File | Status | Change |
|---|---|---|
| `backend/instance_lock.py` | modified (S5, tested here) | shared detector `_detect_gap_locked`; `clock_gap_check`; `resume_seq`; `take_unnoted_resume`; `consume_resume`; `heartbeat_once` uses the shared detector; `run_heartbeat_loop` consumes any unnoted `resume_seq` |
| `backend/protection_runtime.py` | modified (own file) | `may_increase_exposure` (DD-9, conditions 1–7, each reported); `recover_journal` (rows 3/10, stale EE tickets) |
| `backend/reconciliation.py` | modified (Edit, +1 function, +1 call) | `reset_memory_if_resumed()`; called first in `_run_cycle` (under `CLASSIFICATION_LOCK`) |
| `backend/startup_reconciliation.py` | modified (Edit, 3 small hunks) | G4 call inside Stage 1's `CLASSIFICATION_LOCK` block; `open_protection_operations` in the local inventory → `UNRESOLVED_OPERATION` (empty on a database without the journal table, so unchanged there) |
| `tests/test_protection_s8.py` | new | 27 tests |

## 2. R2-7 point by point

| Point | Implementation | Test |
|---|---|---|
| 1 one shared baseline, exactly one detection per gap | `_detect_gap_locked` reads and refreshes `last_hb_wall/mono` in one `cv` section, for the heartbeat and the engine alike | C01, C02, C05 (8 threads, one `resume_seq`) |
| 2 the heartbeat consumes a gap it did not detect | `consume_resume()` (note_resume + observations reset once per `resume_seq`), called every loop iteration | C03 |
| 3 gating on the shared flag | `send_gate` refuses every non-`none` class while `RESUMED_AFTER_SUSPEND` is set; a second gap moves `resumed_at` so a running Stage 1 cannot clear it | C04, C06 |
| 4 lock order | the CAS takes `cv` inside its DB lock only (DB → cv); never `CLASSIFICATION_LOCK`, never a second connection | C19 (static + runtime spy) |
| 5 what the detecting CAS writes | `intent → not_sent` (row 25) or `unknown` unchanged; no broker call | S5 T12, C08 |
| 6 epoch before clock | the fence raises `FencingError` before any clock logic; `resume_seq` unchanged | C11 |
| 7 heartbeat and CAS share only the DB lock | as before; a blocked heartbeat sets `HEARTBEAT_STALE` after the lease, which DD-9 refuses | C16 (4) |
| 8 pre-call re-check | inside the held fenced transaction B (S5): a gap → no call, op stays `sending`, recovery → `unknown` | S5 T17 |

Cleanup after the DB write: `gtt_observations.reset()` and `note_resume` run from `consume_resume()` on the heartbeat
loop, never inside a CAS transaction. Authority once lost is never regained in the process (C12).

## 3. Clocks

| Case | Behaviour | Test |
|---|---|---|
| normal runtime / gap below 3 × heartbeat | nothing | C02 |
| long pause, sleep, hibernate (hours) | one resume; reads taken before the wake refused for every class (DD-8 iii) | C01, C09 |
| clock moving forward (NTP step) | detected as a gap: resume path, sends refused; settle spacing also needs the monotonic difference (S6 R08) | C08 |
| clock moving backward | not a "gap"; every read then has a negative age < −60 s → `READ_STALE`, sends refused (fails safe) | C07 |
| multiple detectors together | exactly one `resume_seq` | C05 |
| delayed response after a resume (same epoch) | associated by M-4 | C10 |
| ownership loss during resume | `FencingError` first, role `lost` | C11 |

## 4. Recovery

`recover_journal` (journal-aware Stage 1, fenced, idempotent): an older-epoch `intent` → `not_sent` (row 3); an
older-epoch `sending` → `unknown` with the settle count reset (row 10; never re-sent); an older-epoch Emergency Exit
ticket that was never submitted → `cancelled` (ownership released); a submitted one is never released (C13).
Unknown broker state after recovery concludes nothing and allows no create (C14). DD-9: passes on a clean account
and ignores `UNRESOLVED_OPERATION` on other positions (C15); each of the seven conditions failing alone refuses
(C16 ×7). G3 inventory (C17); G4 consumer-driven reset after a CAS-detected resume (C18).

## 5. Mutations (11, all caught)

clock check removed from the CAS · heartbeat notes only its own detections · CAS does not refresh the baseline ·
`clear_resume` clears a newer resume · G4 call removed · G4 ignores `resume_seq` · recovery keeps an older intent ·
recovery releases a submitted EE SELL · DD-9 replaced by "every restriction reason" (recovery deadlock) · G3 removed ·
recovery leaves an older `sending`.

## 6. What only a controlled physical Windows machine can validate (WIN-INT)

Real S3/S4 sleep and hibernate wake ordering (which of the heartbeat, a loop timer, a route or the engine runs first);
`time.monotonic()` behaviour across sleep on the target hardware (U3 answered for one laptop only); the SQLite lock and
WAL state of a process frozen by a real suspend; antivirus/indexer handle interference; NTP step behaviour of the
Windows time service. The engine is fail-closed in each case (both clocks are checked; reads before the resume are
refused), but the timing itself is not proven by these tests.
