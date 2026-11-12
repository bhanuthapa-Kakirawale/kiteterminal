# Phase 3 Stage S9: validation report (integration and release-readiness validation)

**Date:** 2026-11-11 · **Branch / HEAD:** unchanged (`35a0e07f…`); nothing staged, committed, pushed or stashed

**Update (same day): the user chose option A (§3). After the change the full regression is green — exit 0, 4184
collected, 4178 passed, 0 failed, 0 errors, 6 skipped, 0 orphan processes (§6). S9 is ACCEPTED.** The text below
records the first run and the stop as they happened.

**Original result: S9 NOT ACCEPTED — stopped on a design conflict that needs a user decision (§3).** Every engine test,
multi-process test, repeated race run, mutation check and integrity check passes. The full standing regression has
**2 failures**, both pre-existing structural invariants that the approved R6 engine cannot satisfy without either
editing the frozen `execution.py` or relaxing an existing safety test. Neither was done. One harness defect found by
the run was fixed (§4).

## 1. Full standing regression

```
PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest -rs -p no:cacheprovider -o faulthandler_timeout=300 \
  --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py --junitxml=...
```

**Exit 1. 4178 collected: 4169 passed, 2 failed, 1 error, 6 skipped, in 1040 s.** (JUnit XML; 4178 = the 3878
baseline + the 300 engine tests present when the run started.)

| Item | Test | Cause | Status |
|---|---|---|---|
| FAILED | `test_live_entry_gate.py::test_15_only_the_frozen_order_path_places_orders_or_gtts` | `emergency_exit_engine._sell` calls `place_order`; `protection_send._call` calls `place_gtt` (the test stops at its first attribute, so both offend) | **design conflict, §3** |
| FAILED | `test_dark_protection_schema.py::test_11_no_application_module_refers_to_the_dark_schema` | the engine modules (and the S8 G3 Stage 1 inventory) name the dark schema, by design | **design conflict, §3** |
| ERROR | `test_mp_harness_selftest.py::test_H3…` teardown: "processes still alive: [23840]" | harness defect, not the engine: §4 | **fixed**; 3/3 reruns clean |
| SKIPPED (6) | `test_dmb_inbox.py:384` ×3 (symlink privilege), `test_instance_lock.py` WIN-INT ×3 | environment | reasons still valid |

Guard: parent `unexpected 0`, network refusals 1 (the expected one); children 155 reports, `unexpected 0`, protected
opens 0, missing reports 0.

## 2. What passes

| Evidence | Result |
|---|---|
| In-process engine suites (S4 157, S5 36, S6 41, S7 32, S8 27, static 6) | **299 passed** (JUnit) |
| Multi-process engine tests `test_protection_mp.py` (PE1 stale owner; PE2 takeover blocked mid-send; PE3 crash / dropped response → adoption, no resend; PE4 held response to a dead owner; PE5 claim races; PE6 two-process EE clicks → one SELL) | 7 passed |
| Repeated races: MP engine + S3 MP-L1/MP-C1/MP-E1/residuals | 5/5 runs; 45 child reports each; 0 unexpected; 0 orphans |
| After the harness fix: harness self-tests + MP engine + static | 3/3 runs; 69 child reports each; 0 orphans |
| Mutation testing S4–S8 | 71/71 caught (2 initial survivors closed with new tests) |
| Static: broker writes only from the two fenced paths; engine imported by no live module; no credential/SDK access; every transition row reachable; production switches unchanged; every journal writer requires the fence | 6 passed |
| Transaction rollback / partial writes / crash boundaries | S4 T10–T11; S5 T30; S7 E19 (×4); MP PE3–PE4 |
| Migration/rollback on temp DBs; DB integrity | existing `test_schema_v11.py`, `test_schema_v12.py`, `test_dark_protection_schema.py` (except test_11) pass; MP-L4 integrity_check |
| Write-route authorisation coverage | `test_write_authority_routes.py` passes (no route added) |

## 3. The design conflict (needs a decision)

`tests/test_live_entry_gate.py` §"structure: one way to the broker" makes the frozen `execution.py` the **only**
place that may call `place_order` / `place_gtt` / `modify_order` (test_15) and `routes/tickets.confirm_and_execute_ticket`
the only caller of `execute_ticket` (test_14). The approved R6 engine sends GTT creates/modifies/cancels itself
(DD-5, M-3, M-4: the fenced send) and decides the Emergency Exit sale (M-7). There is no implementation of R6 that
keeps test_15 green without editing the frozen `execution.py` (forbidden) or relaxing test_15 (weakening a safety
assertion). Routing the call through an injected callable to escape the AST check would defeat the invariant's
purpose and was not done. Equally, test_11 encodes the Phase 1 rule "workflows leave the schema unused", which the
engine (and the S8 G3 inventory R6 requires) necessarily breaks.

**Options for the user:**

| Option | Effect | Recommendation |
|---|---|---|
| A | Amend test_15 to allow exactly the two fenced engine writers (`protection_send._call`, `emergency_exit_engine._sell`), keeping the assertion that every other backend module is excluded, and add that no live module imports the engine (already `test_protection_s9_static.py` ST1/ST2). Amend test_11 to "only the engine modules and the read-only G3 inventory refer to the dark schema". | **Recommended** for the dark phase: the invariant's goal (no unguarded second path to the broker) is kept and enforced more precisely; the engine is unreachable from any live path. |
| B | Keep test_15 as is; restructure the engine so it only *decides*, and route every engine broker write through `execution.py` in Phase 4 (frozen-file change, PK-25 supersede record). | Correct long-term shape for live wiring; it needs the Phase 4 authorisation, and until then the suite stays red or the engine's send path is removed. |
| C | Remove the engine's broker-write paths until Phase 4. | Loses the S5 fenced-send evidence. Not recommended. |

Until a decision, **S9 is not accepted** and the regression is not green on this worktree.

## 4. Harness defect found and fixed

The teardown's descendant snapshot (`tests/mp_harness.descendants`) followed Toolhelp parent links without checking
creation times. Windows keeps a dead parent's pid as `th32ParentProcessID` and reuses pids, so an **unrelated,
older** process (pid 23840, `mc-fw-host`, started hours before the run; security software) was taken for a
descendant of a harness child, and teardown tried to **terminate** it. It survived (protected) and was reported as an
orphan. Fix: a process is a child only if it was created at or after its parent; a process whose creation time cannot
be read is not claimed (the orphan check still reports any real leftover). H12 (real grandchild reaping) still passes;
3/3 reruns with 0 orphans. The test-infrastructure risk existed since S3 (disclosed there as "PID reuse"); it is now
closed for this case.

## 6. Option A (user decision) and the rerun

- `tests/test_live_entry_gate.py::test_15`: every backend module except frozen `execution.py` is still forbidden to call
  `place_order` / `place_gtt` / `modify_order`, apart from exactly `protection_send._call` (`place_gtt`) and
  `emergency_exit_engine._sell` (`place_order`); the test also fails if either allowed call disappears.
- `tests/test_dark_protection_schema.py::test_11`: only the seven engine modules may name the dark schema, plus four exact
  read-only G3 lines in `startup_reconciliation.py` (each must still exist); the frontend check is unchanged; and no
  backend module outside the engine may import it.
- Pre-change copies of both files are in the session scratchpad.
- Mutations proving the narrowed tests still enforce the invariant (originals restored, sha256 verified): a broker
  write from another engine function → test_15 fails; `routes/kill_switch.py` importing the engine → test_11 fails;
  a new dark-schema reference in `reconciliation.py` → test_11 fails.
- Focused: the two amended modules + static checks, 323 passed.
- **Full standing regression (same command): exit 0 — 4184 collected, 4178 passed, 0 failed, 0 errors, 6 skipped**
  (the six known environment skips). Guard: parent `unexpected 0`; children 155 reports, `unexpected 0`, protected
  opens 0, missing reports 0, **orphan processes 0** (the harness fix held).

## 5. Integrity (after all changes)

Re-checked after option A: PK-25 13/13 · `execution.py` identical to HEAD · `LIVE_ENTRIES_ENABLED = False` ·
`MIGRATIONS` ends at v11 · nothing staged, no stash · branch and HEAD unchanged · worktree 112 → 136 entries: 24 added
and 8 changed by this programme (option A changed two pre-existing test files); the other 104 pre-existing entries are
byte-identical to the pre-change snapshot · edited files keep consistent line endings · no process left running.
