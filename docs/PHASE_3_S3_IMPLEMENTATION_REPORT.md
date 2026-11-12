# Phase 3 Stage S3: implementation report (multi-process FakeKite test harness)

**Date:** 2026-11-10 · **Branch:** `feature/signal-detail-and-strategy-revision` · **HEAD:**
`35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (unchanged; nothing staged, committed, pushed or stashed)

**Result:** S3 is implemented and verified. See §7 for the test results and §11 for the status statement.

- **Tests and docs only.** No `backend/` file was changed.
- **Not registered:** migration v12.
- **Not implemented:** the protection engine.
- **Unchanged:** `LIVE_ENTRIES_ENABLED` stays `False`.

---

## 1. Scope and authorisation

The user authorised Stage S3 with every recommendation of the approved readiness plan:

| # | Decision |
|---|---|
| C1 | Full RR §13 S3 scope: shared multi-process FakeKite, day rollover, clock injection, the FakeKite models |
| C2 | Loopback HTTP JSON-RPC transport |
| C3 | Keep the characterization tests of today's residuals, named `test_residual_*` |
| C4 | One mutation check may temporarily edit `backend/instance_lock.py` (scratch copy, sha256 restore) |
| C5 | A small `tests/conftest.py` edit that aggregates the child guard reports |

- **Plan:** `C:\Users\thapa\.claude\plans\pasted-content-id-f133-claude-code-foamy-sutton.md`.
- **Backup:** the user chose a scratchpad snapshot over a git tag and branch. Before the first edit, the
  session scratchpad received copies of the three pre-existing files to be modified, plus the sha256 of all
  104 worktree entries.

## 2. Files

| File | Status | Purpose |
|---|---|---|
| `tests/child_bootstrap.py` | new | The preamble of every S3 child: guard, dotenv, `KITE_*` scrub, open recording, JSON guard report, path checks |
| `tests/shared_fake_kite.py` | new | Shared-broker server (as a script) and `RemoteFakeKite` / `Controller` clients (as a module) |
| `tests/mp_app_child.py` | new | One application process, driven by JSON-line commands |
| `tests/mp_harness.py` | new | Parent orchestration: `Child`, `Server`, `AppChild`, `Harness`, the `mp` fixture, Job Object, suspend/resume, process snapshot, cleanup |
| `tests/test_mp_harness_selftest.py` | new | H1–H16 (the RR exit gate) |
| `tests/test_mp_coordination.py` | new | MP-L1–L5, E1–E5, S1–S4, C1, F2L, D1–D3, W2, W6 |
| `tests/test_fake_kite_models_s3.py` | new | The new FakeKite models |
| `tests/fake_kite.py` | modified (additions only; the existing uncommitted edits preserved) | Opt-in models and the rollover clock |
| `tests/conftest.py` | modified (two hooks extended) | Child guard summary line; session failure on child isolation problems |
| `docs/TESTING.md` | modified (new §1.7) | The harness |
| `docs/PHASE_3_S3_IMPLEMENTATION_REPORT.md` | new | This report |

**Not modified:**
- every `backend/` file, including `instance_lock.py`, `execution.py` and the 13 PK-25 files;
- `tests/instance_lock_child.py`, `tests/test_instance_lock.py` and `tests/real_data_guard.py`;
- `MIGRATIONS`;
- `backend/live_entry_gate.py`.

All new files are CRLF, like the rest of the repository. The edited files kept their CRLF endings.

## 3. Architecture

```
pytest (parent guard; tmp_path; Harness)
 ├─ shared FakeKite server   child_bootstrap → 127.0.0.1:0 → "ready host port pid"
 │     one FakeKite, one lock; global log: seq, pid, tag, epoch, method, effect_applied, response
 ├─ app child A              child_bootstrap → mp_app_child → fenced RemoteFakeKite ─┐
 └─ app child B              child_bootstrap → mp_app_child → fenced RemoteFakeKite ─┴→ server
```

- **Serial log.**
  - The server assigns a call's sequence number and applies its effect in **one** locked section, so the log
    order is the effect order. H3 asserts this.
  - Only `hold_before` separates the two, on purpose: logged on arrival, processed later (late processing).
  - A held response never holds the lock.
- **Faults are one-shot per method:** `raise_before`, `accept_then_raise` (e.g. `SimulatedCrash`, which makes
  the child `os._exit(137)` after the broker took effect), `accept_then_drop`, `accept_then_hold`,
  `accept_then_malformed` (→ `DataException`, what the SDK raises) and `hold_before`.
  - Gates release themselves after `max_hold_seconds`, so nothing can hang forever.
  - Exceptions cross by class name from an allow-list only; anything else is a `HarnessError`.
- **`RemoteFakeKite` has no `__getattr__` fallback.** Every write it can make is in
  `kite_client.BROKER_WRITE_METHODS`, so `fence_broker` wraps all of them (H16).
- **Application children:**
  - They patch `kite_client.get_kite` to the fenced client and make `create_kite_connect` raise.
  - They record Stage 1 requests instead of scheduling them.
  - They scale timing in their own process only: heartbeat 0.2 s, busy timeout 1 s, takeover poll 10 ms.
- **The lease stays at 15 s:** `app_instance` has `CHECK lease_seconds BETWEEN 15 AND 600`. A successor that
  must not wait shifts its wall clock forward, as Phase 2 E2/P1 did.
- **Lock bypass:** a different lock file for the same database, the Phase 2 E2/P1 simulation. With a working
  OS lock it is the only way two processes on one machine can both take over.

### 3.1 Two Windows findings that shaped the harness

1. **The venv `python.exe` is a launcher.** It runs the real interpreter as its own child, so `Popen.pid` is
   not the process that holds locks and handles.
   - Every child reports `os.getpid()` when ready.
   - `kill_hard` (`TerminateProcess`) and `suspend` act on that pid.
   - Terminating only the launcher would have left the lock holder alive and made crash tests meaningless.
     Phase 2's `os._exit` tests were unaffected, because the launcher propagates the exit code.
2. **The launcher runs the interpreter outside the Job Object** its launcher was assigned to. The Job lists
   only the launchers.
   - Creating the launcher suspended and assigning it before its first instruction did not change that.
   - The suspended-creation variant was then **removed**. Resuming through a thread snapshot occasionally
     left a child suspended for good: two silent start-up stalls, in one repeat run each.
   - The interpreter dies with its launcher, but a grandchild it starts does not. Teardown therefore also
     reaps every descendant found in a Toolhelp process snapshot taken while the parents are alive.
   - `kill_hard` records a child's descendants before they become orphans.
   - The Job remains as a first layer. Mutation M5b confirms it is not load-bearing (§8).

## 4. Tests

### 4.1 Harness self-tests: `test_mp_harness_selftest.py` (35)

H1 loopback bind and refusal · H2 one book for two processes · H3 300 concurrent writes from 3 processes ×
4 threads, serial log in effect order · H3b the same with a 20 ms probe delay inside the server's
seq-and-effect section: at most one request inside at once (a deterministic check of the server lock,
added after mutation M1 survived H3 alone) · H4 every allow-listed exception round-trips (12), broker refusal
type kept, unmapped → `HarnessError` · H5 drop, malformed · H6 hold → timeout, release, auto-release ·
H7 `raise_before`, `hold_before` · H8 no deadlock while held · H9 guard reports · H10 a decoy protected
path and a TEST-NET connect refused in a child · H11 static bootstrap-first check · H12 teardown kills a
child that ignores exit and reaps its grandchild · H13 hung command and dead child diagnostics · H14 EOF
watchdogs · H15 data opens only under `tmp_path` (allow-list: `backend/calendar_data`, `.git` — read-only
repository files the app reads) · H16 every remote write fenced, refused without authority, nothing
reaches the broker.

### 4.2 Coordination: `test_mp_coordination.py` (23)

| ID | Asserts (DB and shared-broker log) |
|---|---|
| MP-L1 | 50 rounds, both processes released together: exactly one owner per round, epoch +1 each round; the loser's write refused `ANOTHER_INSTANCE_RUNNING`; the broker log holds exactly the 50 owner writes, each with that round's epoch |
| MP-L2 | Clean shutdown → `stopped_clean`, `clean_shutdown=1`; successor takes over in < 1 s, `previous_end=stopped_clean`, only `STARTUP_RECONCILIATION_INCOMPLETE` |
| MP-L3 / W1 | `TerminateProcess` of the owner: lock free at once (timing printed); successor waits out the remaining lease, `crashed`, `PREVIOUS_INSTANCE_UNCLEAN`, history `crashed_detected` |
| MP-L4 / W7 | Death inside `BEGIN IMMEDIATE` (`os._exit` and `TerminateProcess`): the uncommitted row is gone, `integrity_check` ok, successor writes |
| MP-L5 / D1 | Death while the `place_gtt` response is held (and released late): successor sees the GTT, exactly 1 broker write (A's), 0 by the successor after Stage 1 |
| MP-E1 | Bypass takeover: the old owner's fenced write → `FencingError` / `INSTANCE_LOCK_LOST`, role `lost`, its broker write refused, 0 broker writes by it |
| `test_residual_MP_E2` | **Residual pinned:** a cycle past `loop_may_run` → fenced write refused, **unfenced write commits** (D§7.3) |
| `test_residual_MP_E3` | **Residual pinned:** a stale owner's fenced-broker write **is sent** before it notices (stale epoch visible in the broker log); a fenced claim first (the engine pattern) refuses the next one |
| MP-E4 | Heartbeat detects a bypass takeover within 2 intervals plus a margin (latency printed) |
| MP-E5 | 20 crash/takeover generations: epochs strictly +1; history 20 × `crashed_detected` + `running` |
| MP-S1 / W3 | A thread-suspended owner keeps the OS lock (a second process is `non_owner`); authority kept after resume |
| MP-S2 | A real 1.5 s freeze of every thread: `RESUMED_AFTER_SUSPEND`, Stage 1 requested, derived `RESTRICTED`; `NORMAL` again after a passing Stage 1 |
| MP-S3 | Bypass takeover during a freeze: the first heartbeat after resume → `lost`; writes refused; 0 broker writes by it |
| MP-S4 / W3 | Owner frozen inside a write transaction: a bypass takeover fails closed (`database is locked` after the 1 s busy timeout), never owner, its broker write refused, epoch unchanged; after resume the owner commits and the takeover then succeeds |
| MP-C1 | Dark schema on the tmp DB; 100 rounds, two processes race an `intent` create per position (shared simulated identity, as Phase 2 S1): exactly one row per position, loser `IntegrityError` with **no** broker call, 100 `place_gtt` total |
| MP-F2L (×2) | F2 on the existing entry path: a committed `submitting` claim, the shared broker accepts the order, then the process dies (`SimulatedCrash` → `os._exit`) or the response is dropped: successor Stage 1 → `UNRESOLVED_OPERATION`, exactly 1 `place_order` (A's), 0 by the successor, claim still `submitting` |
| MP-D2 | Late response delivered to a stale owner: its fenced record is refused (the G2 primitive) |
| MP-D3 | Effects in arrival order, responses delivered in the other order |
| W2 | The lock is not inherited (`O_NOINHERIT`): a grandchild outlives its killed parent, the successor still takes over |
| W6 | After teardown the DB, `-wal`, `-shm` and lock file can be renamed: no leaked handle |

### 4.3 FakeKite models: `test_fake_kite_models_s3.py` (18)

Every model is opt-in, off by default and labelled UNVERIFIED. "Defaults unchanged" is asserted, and the
Phase 0 suite `test_fake_kite_extensions.py` passes unmodified.

| Model | Register |
|---|---|
| `repeat_delete_semantics` / `repeat_modify_semantics` (`noop` = existing behaviour, `error`) | B-27 / G5 |
| `place_gtt_refuse_until_holding_visible` (holdings + T1 + day CNC net) | B-02 |
| `trigger_gtt(ddpi_rejected=True)` | B-05 / U1 |
| `place_gtt_creates_duplicate` | B-24 (R5-1 T4) |
| `hide_triggered_order_calls` | B-13 (R6 T6) |
| `deleted_gtt_listing="pruned"` | B-28 / B-08 |
| `gtt_listing_limit` (single page) / `gtt_listing_omit_ids` (partial listing) | B-08 / M-10 |
| `rollover(days)`: T1 settles, day net → T1, orders leave `orders()` but stay in `order_history()`, positions cleared, GTTs kept, optional expiry, clock +1 day | proposal §9.2 item 6 |

## 5. Isolation and safety guarantees (as built)

- Every child imports `child_bootstrap` first (H11). It installs the guard, neutralises dotenv, scrubs
  `KITE_*` and records opens.
- The child refuses to start if any configured path is outside the test's `tmp_path`, is protected, or has a
  protected lock path.
- Each child writes a guard report: before it reports ready, after every command, and at exit (an `os._exit`
  crash writes it first).
- The `mp` fixture fails a test on any unexpected child refusal or protected open.
- conftest prints the child summary and fails the session on an unexpected child refusal, a protected open,
  a missing report or an orphan process.
- The server binds `127.0.0.1` only (H1). The network guard is active in every child (H10).
- No SDK client can be built in an application child. Credentials are the env fixture's sentinels;
  `KITE_REST_PROXY` is the loopback discard address and is never contacted.
- No test touches `backend/kite_terminal.db*`, `.env*`, `ui_prefs.json` or the real lock file. H15 lists
  every data file the children opened.

## 6. Failure injection, and what cannot be reproduced

**Injected:**
- crash before or after the effect (`os._exit`, `TerminateProcess`);
- dropped, held, late, reordered and malformed responses;
- session expiry and outage on every call (through `/control`);
- listing lag, partial or single-page listing, pruned deletes, hidden triggered orders;
- duplicate create;
- busy timeout;
- real thread suspension;
- clock offsets;
- lock bypass.

**Not reliably reproducible (disclosed):**
1. the R2-7 point 8 window between the last in-process check and the bytes leaving the socket;
2. proxy-level replay or duplicate delivery (U6);
3. real Kite processing after a client timeout (B-31), and true broker-side concurrency (the server serialises);
4. true simultaneity of lock acquisition: repetition gives statistical evidence, never proof;
5. real sleep/hibernate wake ordering (manual WIN-INT only);
6. bypass across machines or network shares;
7. inter-machine clock skew;
8. power loss and torn writes;
9. bytecode-level interleavings;
10. antivirus or indexer handle interference.

## 7. Test results

Project venv, sequential runs (no xdist). Counts are from pytest's summary and, for the full run, `--junitxml`.

### 7.1 Focused, repeated (acceptance criterion 1)

`scratchpad/repeat.py`: the three S3 modules 3 times, then `-k "MP_L1 or MP_C1 or H3"` 10 times, stopping at
the first failure. The final cycle passed **13 of 13**:

| Runs | Result | Time | Child guard summary (each run) |
|---|---|---|---|
| focused × 3 | 75 passed each | 142–151 s | 130 reports; refusals 2 (decoy 1, expected 1, **unexpected 0**); protected opens 0; missing reports 0; orphans 0 |
| race × 10 | 3 passed each | about 17 s | 10 reports; unexpected 0; orphans 0 |

**Earlier cycles failed, and each failure was fixed (disclosed):**
1. **MP-L3 lease bound.** The test assumed a fresh predecessor heartbeat, but the successor's own start-up had
   used part of the lease (waited 0.30 s < 0.5 s). B's clock shift is now computed from the recorded
   `heartbeat_at`; the wait is then 1.99 s every time.
2. **Silent start-up stalls.** Twice, a child produced no output for the whole readiness deadline (60 s,
   then 120 s); the second stalled run took 11 min.
   - Both appeared only after the launcher was created suspended, then assigned to the Job and resumed
     through a thread snapshot.
   - Suspended creation was removed (§3.1). No stall has occurred since, and run times dropped to the
     figures above.
   - The readiness deadline stays at 120 s (`mp_harness.READY_DEADLINE`).

H3b and the server's probe (§8, M1) were added **after** that cycle. The final files are covered by the
focused H3/H3b run and by the full regression (§7.3).

### 7.2 Neighbouring suites

`test_instance_lock.py test_fake_kite_extensions.py test_harness_isolation.py test_real_data_guard.py
test_broker_write_fence.py test_startup_reconciliation.py`: **186 passed, 3 skipped** (the WIN-INT
placeholders). Guard refusals 19 (decoy 15, expected 4, **unexpected 0**).

### 7.3 Full standing regression (final files)

```
PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest -rs -p no:cacheprovider -o faulthandler_timeout=300 \
  --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py
```

**Exit 0. 3878 collected: 3872 passed, 0 failed, 0 errors, 6 skipped, in 1319.8 s.**

- **Collected:** the S2 baseline of 3802, plus 76 new (35 + 23 + 18).
- **Skips (6, all pre-existing):** `test_dmb_inbox.py:384` × 3 (symlink privilege) and the three WIN-INT
  placeholders.
- **L5:** `test_instance_lock.py::test_L5_the_real_lock_file_is_guarded`, the one S2-era failure, passes after
  the L5 correction.
- **Guard:** `guard refusals: 19 (decoy 15, expected 4, unexpected 0); network refusals: 1`.
- **Children:** `child processes (S3 harness): 134 guard reports; refusals 2 (decoy 1, expected 1, unexpected 0);
  protected opens 0; missing reports 0; orphan processes 0`.
- **Exclusions:** only the two standing DMB exclusions.

## 8. Mutation checks

Script: `scratchpad/mutate.py`.
- Each mutation is an exact-once text replacement.
- The target tests then run.
- The original bytes are restored in `finally`, with the sha256 verified after every mutation (all OK).
- "Caught" means exit 1 with test failures, not a collection error.

| # | Mutation | Target | Result |
|---|---|---|---|
| M1 | Server lock removed (`nullcontext`) | H3 | **survived** at first: with fast calls the GIL rarely interleaves requests. H3b was added |
| M1 | the same | H3b | **caught** (1 failed) |
| M2 | Client maps a dropped response to success | H5, H6 | caught (2 failed) |
| M3 | App child installs the guard late (bootstrap import after a backend import) | H11 | caught (1 failed) |
| M4 | Fence bypassed in the app child | MP-L1 | caught (1 failed) |
| M5a | Descendant reaping removed from teardown | H12 | caught (1 failed). The surviving sleeper was cleaned up by the script |
| M5b | Job Object disabled | H12 | **survived, as expected**: the Job is not load-bearing under the venv launcher (§3.1); M5a is the load-bearing check |
| M6 | **Production** `fenced_transaction` epoch check removed (C4; `backend/instance_lock.py` restored, sha256 OK) | MP-E1, MP-D2, `test_residual_MP_E3` | caught (3 failed) |

**Not run:** "inheritable lock fd", because it would be a second production mutation (C4 allows one); see §10
item 8.

## 9. R6 items: verified here, primitive only, or later

| R6 item | S3 status |
|---|---|
| Two processes competing for the lock; release on clean stop and on death; WAL recovery | **Verified** (MP-L1–L5, W1, W7) — for the existing Phase 2 mechanisms |
| Fencing epoch across processes; stale owner's DB writes | **Verified** for fenced writes (MP-E1, D2, S3); unfenced writes pinned as a residual (MP-E2) |
| Stale owner's broker requests | **Residual pinned** (MP-E3): `broker_write` checks the in-memory role only; the fenced-claim-first pattern is shown to stop it. Engine enforcement: S5/S8 |
| Suspend/resume, loss of authority | **Verified** for the existing heartbeat path (MP-S1–S4, real thread suspension, scaled clocks) |
| Duplicate broker writes | **Verified** at the schema level (MP-C1) and on the existing entry path (MP-F2L); the protection-op F2 is S5 |
| M-4 late-response association (`op_id`/`op_uuid`/`attempt` CAS) | **Primitive only** (MP-D2, D3); S5 |
| DD-10 G2 log line and adoption; G4 `resume_seq` reset | **Primitive only** (MP-D2); S5/S6/S8 |
| M-6 / R2-7 `clock_gap_check`, exactly-once detection, pre-call re-check | **Not verifiable yet** (no primitive in the code); harness clock injection and suspension ready; S8 |
| M-5 at most one GTT per position across a takeover | **Primitive only** (shared FakeKite plus takeover orchestration); S6/S8 |
| DD-9, DX, M-1/M-2/M-3, Q1 | Engine logic; S4–S7 (B-27 models ready) |
| M-7 Emergency Exit gate (G_max, S_agg, T(m)/O(m), T1–T6) | Engine; S7 (B-24, B-13 and multi-leg models ready) |
| B-02/04/05/08/13/24/27/28/31/32 | **Models only; Phase 9** for evidence |

## 10. Known limitations

1. **Models only.** FakeKite and the fault modes are models; no test here says anything about Kite.
2. **Fidelity.** The RPC is method-level, so the SDK `_fenced_request` and the U6 no-redirect path are not
   exercised across processes. Both stay covered in-process (E5, `test_u6_redirects.py`).
3. **Job Object.** The Job does not contain the venv interpreter subtree (§3.1). Cleanup relies on descendant
   reaping. A grandchild orphaned before teardown is reaped only if the test recorded it (`mp.track`, or
   `kill_hard`'s snapshot).
4. **PID reuse.** Teardown terminates recorded pids that are still alive. A pid reused by an unrelated
   process within the seconds of one test is a theoretical risk.
5. **Timing.** Timing assertions use generous margins (E4, L3, S2) and print the measured values. A heavily
   loaded machine could still exceed them.
6. **Runtime.** The three S3 modules take about 2.5 min (142–151 s in the final repeat cycle). The full run
   took 1320 s, against S2's 918 s: that difference includes machine-load variance as well as the S3 suites.
   Python start-up plus the backend import is about 1 s per child on this machine.
7. **Real application.** The real `backend/kite_terminal.db.instance.lock` exists (S2 §6.4). It was not
   accessed. Run the S3 suites with the real app stopped.
8. **Omitted mutation.** The plan's "inheritable lock fd" mutation would edit production `instance_lock.py`
   beyond the one production mutation C4 allows. It was not run; W2 tests the property positively.

## 11. Integrity and status

Read-only checks after the final edits (`scratchpad/integrity.py`, git with `--no-optional-locks`):

| Check | Result |
|---|---|
| Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07f…`, unchanged |
| Staged / stash | empty / empty. No commit, push, reset, clean, checkout or branch switch |
| Inventory | 104 → 112 entries: exactly the 8 authorised new files; none removed |
| Pre-existing entries | sha256 identical to the pre-edit snapshot for all 104 entries except the 3 authorised modifications: `tests/fake_kite.py`, `tests/conftest.py`, `docs/TESTING.md` |
| `backend/` | not touched (no added or changed entry). M6 edited `backend/instance_lock.py` temporarily; it was restored and its sha256 verified |
| `backend/execution.py` | identical to HEAD |
| PK-25 frozen hashes | **13 / 13** match |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py`) |
| Migrations | `MIGRATIONS` ends at `(11, "phase2_instance_coordination", …)`; **v12 not registered** |
| Processes | no harness or test Python process left running |

**Safety confirmation.**
- **Protected data not accessed:** `.env*`, credentials, the account database, its backups, the real lock file
  and `backend/ui_prefs.json`. The guard reported 0 unexpected refusals in the parent and in every child.
- **No broker or network contact:** no Kite MCP tool and no external network request. Only loopback was used,
  by the harness.
- **The real application was never started.**
- **Every database was a pytest temporary database**, including every takeover and the dark schema of MP-C1.

**Status: S3 complete.** The harness self-tests pass, the guard summary is `unexpected 0` in the parent and in
every child, the full regression is green, and all mutations except M5b (expected) are caught.

S4 (the protection engine), v12 registration and live activation each remain separately authorised.
