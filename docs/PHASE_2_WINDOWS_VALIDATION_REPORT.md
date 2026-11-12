# Phase 2 Windows integration validation: L3, L6, R8

This report covers the three WIN-INT items that `docs/PHASE_2_IMPLEMENTATION_REPORT.md` §10 left
PENDING:

- **L3:** `subst` drive alias.
- **L6:** a real network share.
- **R8:** a real sleep/hibernate.

It is separate from the Phase 2 implementation report, which was not edited.

Date: 2026-11-08 (IST, UTC+05:30). Runner: `tests/winint_phase2.py` (unchanged). Supplementary checks ran
from session-scratchpad scripts that use the runner's safety setup: the Phase 0 guard is installed
first, only disposable `mkdtemp` folders are used, nothing goes near the account DB or `.env`, and no
broker is involved.

## Summary

| Test | Result | Basis |
|---|---|---|
| **L3 subst-drive** | **PASS** | Runner PASS. Supplementary 50/50: both directions, case, junction, subst + junction, release, no stale lock. |
| **L6 network share** | **PASS (loopback SMB; remote host not tested)** | Runner PASS. Supplementary 56/56 over a genuine SMB share (`\\localhost\C$`, `\\127.0.0.1\C$`, `\\<hostname>\C$`, and a mapped drive), all `DRIVE_REMOTE`. The user approved loopback; a share on another host was not available. |
| **R8 sleep/hibernate** | **PASS** | Real Modern Standby (514 s) and real Hibernate (612 s). Runner PASS and supplementary probe 24/24 in each cycle. A first hibernate attempt was too short (73 s) and is not counted. |

`LIVE_ENTRIES_ENABLED = False`, unchanged. Phase 3 was not authorized or started.

## 1. Environment

| Item | Value |
|---|---|
| OS | Microsoft Windows 11 Home Single Language, 10.0.26300 |
| Machine | Laptop `LAPTOP-DG9K4B1R`. This is the user's own machine, not a dedicated test machine (as with L2 in the Phase 2 report). |
| Shell / privileges | PowerShell 5.1 and Git Bash, **not elevated** (`IsInRole(Administrator) = False`) |
| Python | `.venv\Scripts\python.exe`, 3.13.14 (the project venv; the system Python has no `kiteconnect`, see §2.1) |
| Sleep states (`powercfg /a`) | Standby (S0 Low Power Idle) *Network Connected*, Hibernate, Fast Startup. S1/S2/S3 and Hybrid Sleep are unavailable. |
| SMB | `LanmanServer` and `LanmanWorkstation` running. Shares: the default admin shares `C$`, `D$`, `E$`, plus `ADMIN$`, `IPC$`, `print$` and a printer. |
| Drives at start | `C:`, `D:`, `E:`, `G:`. `subst`: none. `net use`: none ("New connections will be remembered"). |
| App state | No `python`/`uvicorn` process running at the start (the app was stopped) |
| Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` |
| Heartbeat config | `HEARTBEAT_SECONDS = 15` (resume threshold 3 × 15 = 45 s), `INSTANCE_LEASE_SECONDS = 60` |

### 1.1 Baseline integrity (before any test)

Captured by a read-only scratchpad script (`integrity.py`). It never imports `backend`, and it hashes
with CRLF→LF normalization, as `research/pk25/validation_manifest.json` → `model_freeze` does.

| Check | Value |
|---|---|
| `git status --short` | 98 entries (149 with `--ignored`); full lists saved in `integrity_before.json` |
| `git stash list` | empty |
| PK-25 frozen hashes | **13 / 13 match** (2 `model_file_sha256` + 11 `production_file_sha256`) |
| `backend/execution.py` | LF sha256 `0ac964820c30ffe9e6a68e10d34bb17d26f4def27e43c96d63eb7f7b8f74c0bf` (= manifest); raw sha256 `7639648127ea8bfa70a526c15948173643bacdeb58e0b3dd793a8daaa0b141e3` |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, read as text) |

### 1.2 Decisions taken with the user before testing

- **L6:** the only share available is Windows' built-in admin share, reached over loopback SMB. The user
  chose to use it, with the result labelled as loopback and the absence of a remote-host test recorded
  (§4.4).
- **R8:** the user carried out a real Modern Standby and a real Hibernate, waking the machine by hand
  each time.
- **Scratch v11:** `r8-sleep` calls `db.init_db()` on a brand-new temp database, which applies schema
  migrations 1–11 to that scratch file. The user confirmed that the "do not run v11" rule refers to the
  account database. No migration ran on any real database, and v12 does not exist in code.

## 2. L3: subst-drive alias

**Expected behaviour.** Every alias of the database file resolves to one lock file
(`lock_path_for` → `os.path.realpath`), so a second instance reaching the database through a `subst`
drive, a different case or a junction finds the lock held. It becomes `NON_OWNER`, gets no authority
and does not take over. After release, the lock can be taken again through any alias, with no stale
lock.

### 2.1 Runner (documented procedure)

`Q:` was chosen because it was a free letter (only `C: D: E: G:` existed).

```
PS> .venv\Scripts\python.exe tests/winint_phase2.py l3-subst Q:
event=instance.non_owner code=ANOTHER_INSTANCE_RUNNING lock=C:\Users\thapa\AppData\Local\Temp\kt_winint_l3_sd27l48d\alias.db.instance.lock: another process owns this database; this process is read-only (no init_db, no loops, every write refused)
PASS L3 subst alias Q:\alias.db -> role non_owner (lock path resolved to the same file: True)
exit=0
```

Start 17:58:44, end 17:58:46. A first attempt at 17:58:32 used the system `python`. It failed with
`ModuleNotFoundError: No module named 'kiteconnect'` at import, before the `subst` call, so it had
no side effects (`subst` listed nothing afterwards).

The runner removes the mapping (`subst Q: /D`). It does not delete its temp folder, so
`kt_winint_l3_sd27l48d` (two 0-byte files) was removed by hand. Windows refuses to delete a file that is
still locked, so the deletion succeeding also confirms nothing held the lock.

### 2.2 Supplementary checks (`l3_extended.py Q:`): 50 / 50 PASS

The runner checks one direction only: the holder uses the original path and the probe uses the alias.
The supplementary script built a real junction (`mklink /J`) and a real `subst Q:` over a fresh
`kt_winint_l3x_*` folder, then checked six spellings of one file:

| Spelling | Example |
|---|---|
| original | `C:\Users\thapa\AppData\Local\Temp\kt_winint_l3x_p1oaw2eu\Real\alias.db` |
| original, upper-case | `C:\USERS\THAPA\...\REAL\ALIAS.DB` |
| subst | `Q:\Real\alias.db` |
| subst, lower/mixed case | `q:\real\ALIAS.db` |
| junction | `...\Junction\alias.db` |
| subst + junction | `Q:\Junction\alias.db` |

| Group | Result |
|---|---|
| A. Canonical path | All 6 spellings resolve to `...\Real\alias.db.instance.lock`. None of them resolves to a path on `Q:`. |
| B. Holder via **original** (child process) | In-process `acquire()` through each of the 5 other spellings returns `non_owner`, with `authority_ok() = False` and refusal `ANOTHER_INSTANCE_RUNNING`. A second **process** via `Q:` reports `BUSY`. |
| C. Holder via **subst** `Q:` (child process) | Through each of the 5 other spellings: `non_owner`, no authority. A second process via the original path reports `BUSY`. |
| D. Release | Both holders exited (exit 0). Acquiring via `Q:` then gives `locked`, and the recorded `lock_path`/`db_path` are the real `C:\...` paths, not `Q:`. While that lock is held, a child via the junction reports `BUSY`. After release, acquiring via the original gives `locked`. A raw `try_lock_file` succeeds afterwards (no stale lock), and exactly one lock file exists (`alias.db.instance.lock`). |
| Cleanup | `subst Q: /D` (exit 0), junction removed, folder deleted. `subst` lists nothing. |

Start 17:59:31, end 17:59:37. Evidence: scratchpad `l3_extended.log`. The `NativeCommandError` lines in
the raw console output are PowerShell wrapping the expected stderr log lines; they are not errors.

### 2.3 In-suite L3/L6/R8 tests

```
PS> .venv\Scripts\python.exe -m pytest tests/test_instance_lock.py -k "L3 or L6 or R8" -rs -p no:cacheprovider -q
.s........s..s   (11 passed, 3 skipped)
SKIPPED tests\test_instance_lock.py:194: WIN-INT PENDING: L3 subst-drive alias ...
SKIPPED tests\test_instance_lock.py:233: WIN-INT PENDING: L6 against a real network share ...
SKIPPED tests\test_instance_lock.py:674: WIN-INT PENDING: R8 with a real sleep/hibernate ...
guard refusals: 0 (decoy 0, expected 0, unexpected 0); network refusals: 0
```

The three skips are the placeholders this report completes manually. Their skip markers were not
changed, because this task changes no source files.

**L3 verdict: PASS.** Lock acquisition through the original path is detected through the subst alias,
and the reverse also holds. Canonical path handling resolves the subst drive to the real path. Case
variants and junctions, including a junction reached through the subst drive, do not bypass the lock.
The lock releases correctly, leaves no stale lock and records no duplicate authority.

## 3. R8: real sleep / hibernate

**Expected behaviour (design §7.4).**

- The process is paused with the OS lock still held, so no other instance can start while it sleeps.
- The first heartbeat after resume sees a wall-clock or monotonic gap above 45 s and sets
  `RESUMED_AFTER_SUSPEND`.
- On resume it also marks GTT observations stale and requests a Stage 1 re-run, and the account becomes
  `RESTRICTED` until a Stage 1 run passes after the resume.
- Authority is **not** lost, because the epoch is still the process's own.
- After a release, another process can take over (epoch + 1), and the original can write nothing.

### 3.1 Procedure

For each cycle, two background processes started at the same time, each with its own scratch database
in `%TEMP%`:

- the documented runner: `.venv\Scripts\python.exe -u tests/winint_phase2.py r8-sleep`, output to
  `r8_runner_<cycle>.log`;
- the supplementary probe: `.venv\Scripts\python.exe -u r8_extended.py <cycle>`, output to
  `r8_probe_<cycle>.log`, plus one clock sample per heartbeat in `r8_<cycle>.jsonl`.

The probe records five clocks every heartbeat: `time.time`, `time.monotonic`, `time.perf_counter`,
`GetTickCount64` and `QueryUnbiasedInterruptTime`. It does not alter any clock. It replaces
`startup_reconciliation.request_run` with a recorder, as the suite's `env` fixture does, so that
`note_resume()` cannot start a Stage 1 broker read.

The probe's code path was first checked with a `selftest` label that backdates the in-memory
last-heartbeat time, the same technique the suite uses. That run is **not evidence**. Its only failure
was the expected "real suspend ≥ 2 min" check.

After each process reported `ARMED`, the user suspended the machine by hand:

- **Modern Standby:** Start → Power → Sleep.
- **Hibernate:** Win+R → `shutdown /h`.

The user woke the machine with the power button and signed in. No timer, mocked clock or edited
heartbeat was used for any result in this section.

### 3.2 Cycle 1: Modern Standby (S0 Low Power Idle): PASS

Armed at 18:23:57. The last pre-suspend heartbeats were at 18:25:55 (runner) and 18:25:57 (probe).
Resume was detected at 18:34:31 by both processes.

Runner:
```
18:25:55 {"ok": true, "resumed": false, "lost": false} []
event=instance.resumed_after_suspend gap_wall_s=516.8 gap_monotonic_s=516.8: GTT observations marked stale, Stage 1 reconciliation re-runs before new entries
18:34:31 {"ok": true, "resumed": true, "lost": false} ['RESUMED_AFTER_SUSPEND']
PASS R8 resume detected (RESUMED_AFTER_SUSPEND set: True); lock still held across the suspend (second process got 'BUSY'); heartbeat ok after resume: True
```

Probe: **24 / 24 PASS** (details in §3.5).

### 3.3 Cycle 2: Hibernate: PASS (second attempt)

- **First attempt (not counted).** Armed at 18:35:19, with the resume detected at 18:38:17. The gap was
  only **73.3 s** (wall), which includes the 15 s heartbeat interval, so the machine was off for under
  a minute. The procedure requires at least 2 minutes. The runner still printed `PASS`, and every
  functional check in the probe passed (23/24; the failure was the duration check), but this attempt
  does not meet the documented procedure. With the user's agreement it was repeated.
- **Second attempt.** Armed at 18:40:58, with the last pre-suspend heartbeats at 18:41:40 (runner) and
  18:41:28 (probe). The probe detected the resume at 18:51:40 and the runner at 18:51:52.

Runner:
```
18:41:40 {"ok": true, "resumed": false, "lost": false} []
event=instance.resumed_after_suspend gap_wall_s=612.3 gap_monotonic_s=615.8: GTT observations marked stale, Stage 1 reconciliation re-runs before new entries
18:51:52 {"ok": true, "resumed": true, "lost": false} ['RESUMED_AFTER_SUSPEND']
PASS R8 resume detected (RESUMED_AFTER_SUSPEND set: True); lock still held across the suspend (second process got 'BUSY'); heartbeat ok after resume: True
```

Probe: **24 / 24 PASS**.

### 3.4 Clock behaviour across a real suspend (answers U3)

Gap in seconds between the last heartbeat before the suspend and the first one after it (probe
`.jsonl`). A normal heartbeat is about 15.0 s on every clock.

| Cycle | wall `time.time` | `time.monotonic` | `perf_counter` | `GetTickCount64` | `QueryUnbiasedInterruptTime` |
|---|---|---|---|---|---|
| Modern Standby | 514.001 | 514.001 | 514.001 | 514.000 | **7.139** |
| Hibernate (short, not counted) | 73.276 | 76.881 | 76.881 | 73.281 | 15.016 |
| Hibernate | 612.321 | 615.827 | 615.827 | 612.328 | **15.015** |

- **U3 answered on this hardware.** On Python 3.13, `time.monotonic()` (QPC) advances across both Modern
  Standby and Hibernate, so the monotonic check alone would have detected both suspends. Only the
  *unbiased* interrupt time excludes the suspended period. The design checks both wall and monotonic
  clocks, and both clocks exceeded the threshold in every cycle.
- **After hibernate, the monotonic gap was about 3.5 s larger than the wall gap.** The likely cause is
  the wall clock being re-synchronized after resume. This is an inference: the System event log was not
  read, because the user declined that query. It does not affect detection, since either clock
  triggers it.

### 3.5 Supplementary probe results (identical in both counted cycles)

| Aspect required by the brief | Observed (Modern Standby and Hibernate) |
|---|---|
| Lock ownership before suspension | `acquire` gives `locked`, then `takeover` gives `owner` at epoch 1, with authority. A second process reports `BUSY`. The heartbeat is ok. A GTT observation is recorded. |
| Heartbeat during suspension | No heartbeat ran while suspended. The `.jsonl` shows a regular 15 s cadence, then a single gap (514 s or 612 s), then resume detection. Since the OS lock stayed held, nothing could take over in that window. |
| Resume detection | The first heartbeat after wake gives `resumed: true` and sets `RESUMED_AFTER_SUSPEND`. The wall gap was at least 120 s (PASS). |
| GTT observations | Reset: the observation recorded before the suspend is gone (`get(5) is None`). |
| Restriction (fail-closed) | `note_resume()` asked for a Stage 1 re-run (recorded as `['resume']`, **not executed**). The derived state is `RESTRICTED` with `BROKER_READ_STALE`, `RESUMED_AFTER_SUSPEND` and `STARTUP_RECONCILIATION_INCOMPLETE`. The persisted `account_restriction` row has the same reasons (sticky). |
| Authority after resume | Kept, by design: the role is still `owner`, the heartbeat after resume is ok and `lost` is false. The epoch was still the process's own, so there was nothing to lose. New entries are blocked by `RESTRICTED`, not by authority loss. |
| Another process acquiring while the owner is (or was) suspended | Not possible. After resume, a second process still reports `BUSY`, so the lock was held throughout the suspend. |
| Another process acquiring safely after release | After `finish_shutdown`, the shutdown was recorded (`recorded: true`, `in_flight: 0`). A new process (`instance_lock_child.py startup` with boundary `after_takeover`) then took over with **epoch 2**, and `app_instance` shows the successor's epoch and pid. |
| Original prevented from writing after losing authority | The broker-write fence refuses with `INSTANCE_NOT_OWNER` and nothing is sent. `heartbeat_once` does nothing (`ok: false`). A fenced transaction raises `FencingError: INSTANCE_NOT_OWNER: no write authority` and the row is untouched. |
| Regaining authority without restart | Not applicable across a suspend, because authority is never lost. `RESUMED_AFTER_SUSPEND` clears only after a Stage 1 pass that starts after the resume (`clear_resume`). That run needs a broker read, so it was **not executed here**; it is covered only by the simulated suite test `test_R8_a_clock_jump_is_a_resume_that_restricts_until_stage1_passes_again`. A process that actually lost authority (`LOST`/`CONFLICT`) never re-acquires (`instance_lock.acquire`, lines 438–439). That case cannot be produced by sleep; it is covered by the suite's lock-bypass tests (E2/P1), not by this report. |
| Cleanup | The probe removed its scratch folder in both cycles. The runner's folders (`kt_winint_r8_e615zaj0`, `kt_winint_r8_05u2g672`, `kt_winint_r8_by841nht`) were removed by hand after each process exited. |

**R8 verdict: PASS.** A real Modern Standby and a real Hibernate, each of at least 2 minutes, were both
detected. The lock was held throughout, the process failed closed (`RESTRICTED`, GTT observations
stale, Stage 1 requested), authority was kept as designed, a clean handover to a new epoch worked,
and the released original could not write.

## 4. L6: actual network share

**Expected behaviour (design §7.2).** A database path that is not on a fixed local drive (a UNC path, or
a drive whose `GetDriveTypeW` is not `DRIVE_FIXED`) is refused **before** the lock is taken:

- the role is `drive_refused` with `DATABASE_NOT_ON_LOCAL_DRIVE`;
- the process has no authority, every write path refuses, and no lock file is created;
- no instance on the share can become an owner, whatever the lock state.

**Share used.** The built-in `C$` administrative share on this machine, reached over loopback SMB through
the Windows network redirector. This is not a `subst` drive, a junction or a string-only UNC path:

- the file created locally is visible through the share;
- `GetDriveTypeW` returns `4` (`DRIVE_REMOTE`) for every share root and for the mapped drive;
- `os.path.realpath` stays on the share.

The share was only ever pointed at fresh `%TEMP%\kt_winint_l6*` folders. No account data was reachable
through it in any test.

### 4.1 Runner (documented procedure)

```
PS> New-Item -ItemType Directory C:\Users\thapa\AppData\Local\Temp\kt_winint_l6_runner
PS> .venv\Scripts\python.exe tests/winint_phase2.py l6-share '\\localhost\C$\Users\thapa\AppData\Local\Temp\kt_winint_l6_runner'
event=instance.refused code=DATABASE_NOT_ON_LOCAL_DRIVE the database path is a network (UNC) path: \\localhost\C$\Users\thapa\AppData\Local\Temp\kt_winint_l6_runner\kt_winint_l6.db
PASS L6 \\localhost\C$\...\kt_winint_l6_runner\kt_winint_l6.db: role drive_refused; reason: the database path is a network (UNC) path: ...
exit=0
```

Start 18:40:14, end 18:40:15. The folder held **0 files** afterwards, so no lock file was created. The
folder was then removed.

### 4.2 Supplementary checks (`l6_extended.py Q:`): 56 / 56 PASS

Each of four share routes went through the same refusal checks:

- `\\localhost\C$\...`
- `\\127.0.0.1\C$\...`
- `\\LAPTOP-DG9K4B1R\C$\...`
- a mapped drive, `net use Q: \\localhost\C$\... /persistent:no`

| Check (per route) | Result on all 4 routes |
|---|---|
| Share is real | The file is visible through the share. `GetDriveTypeW(root) = 4`. `realpath` stays UNC. For the mapped drive, `realpath` resolves `Q:\l6.db` to `\\localhost\C$\...`. |
| Detection | `local_drive_problem` returns "the database path is a network (UNC) path: ..." |
| Lock refusal | `acquire` returns `drive_refused` with refusal `DATABASE_NOT_ON_LOCAL_DRIVE`. `authority_ok() = False` and no OS lock handle is held. |
| Fail-closed write paths | The broker fence raises `InstanceNotOwner(DATABASE_NOT_ON_LOCAL_DRIVE)`; nothing is sent. `takeover` raises `FencingError`. `require_write_authority` returns `503 DATABASE_NOT_ON_LOCAL_DRIVE`. `heartbeat_once` does nothing. |
| Lock files | None created in the share folder |

No-bypass checks across processes:

| Scenario | Result |
|---|---|
| A local owner holds the lock via `C:\...\l6.db`, and a **separate process** calls `acquire()` on the same file via `\\localhost\C$\...` | `drive_refused`, no lock handle |
| The same, via the mapped `Q:\l6.db` | `drive_refused`, no lock handle |
| Lock **free**, separate process via the share | Still `drive_refused`: the refusal is not first-come-first-served and does not depend on the lock state |
| Control: the same file via the local `C:\` path | `locked` (accepted) |

Cleanup: `net use Q: /delete /y` (exit 0), folder removed, and `net use` lists nothing. Start 18:40:25,
end 18:40:33. Evidence: scratchpad `l6_extended.log`.

**One side effect, found and undone.** `net use ... /persistent:no` also changed the per-user default
from "New connections will be remembered" (the baseline) to "will not be remembered". It was restored
with `net use /persistent:yes`, and `net use` again shows "New connections will be remembered."

### 4.3 L6 verdict: PASS (loopback SMB share)

- **Detection:** the network-share path is detected. Two independent signals agree: the UNC check and
  `DRIVE_REMOTE`.
- **Refusal:** lock acquisition is refused as designed.
- **No bypass:** a second instance on the share cannot get past the refusal, whether or not a local
  owner holds the lock.
- **Local-drive requirement:** enforced. The same file through `C:\` is accepted.
- **Fail-closed:** every write path refuses, the heartbeat does nothing, and no lock file is created.

### 4.4 Not covered by L6

- **A share served by another host** was not available. Loopback SMB goes through the same client
  redirector and returns the same drive type as a remote share. The refusal never reaches the network
  lock itself, so cross-host byte-range-lock semantics do not matter for this check. Even so, a share on
  another machine was not tested.
- To close this fully, run `python tests/winint_phase2.py l6-share \\<other-host>\<share>\<dir>`. That
  needs a writable SMB share on a second Windows machine or a NAS.
- NAS, DFS, WebDAV and cloud-sync virtual drives were not tested. Drive `G:` exists on this machine; its
  type was not examined in this task.

## 5. Process, lock and resource cleanup evidence

| After | Processes (`Win32_Process`, python/uvicorn) | Temp resources | Locks / mappings |
|---|---|---|---|
| L3 runner + supplementary | none | `kt_winint_l3_sd27l48d` removed by hand; `kt_winint_l3x_*` removed by the script | `subst`: empty. Deleting the lock file succeeded, so the lock was not held. |
| R8 standby | none (both background tasks exited 0) | `kt_winint_r8x_standby_*` removed by the probe; `kt_winint_r8_e615zaj0` removed by hand | The runner's `finish_shutdown` ran; the folder deletion succeeded. |
| R8 hibernate (short) | none | `kt_winint_r8x_hibernate_*` removed by the probe; `kt_winint_r8_05u2g672` removed by hand | same as above |
| L6 runner + supplementary | none | `kt_winint_l6_runner` and `kt_winint_l6x_*` removed | `net use`: empty. "Remembered" default restored. |
| R8 hibernate (counted) | none (both tasks exited 0) | `kt_winint_r8x_hibernate2_*` removed by the probe; `kt_winint_r8_by841nht` removed by hand | `subst` and `net use`: empty |

One pre-existing folder, `%TEMP%\kt_winint_l2_a48z97_2` (from an earlier session, 11:51), was left
untouched.

Scratchpad evidence (session-local and not committed):

- `integrity_before.json`, `integrity_after.json`
- `l3_extended.log`, `l6_extended.log`
- `r8_runner_{standby,hibernate,hibernate2}.log`, `r8_probe_{standby,hibernate,hibernate2}.log`
- `r8_{standby,hibernate,hibernate2}.jsonl`
- the scripts `integrity.py`, `compare.py`, `l3_extended.py`, `l6_extended.py`, `r8_extended.py`

## 6. Blocked tests

None. All three tests ran against the real mechanism: a real `subst` drive, real SMB and a real
suspend. The scope limits are stated in §4.4 (L6: loopback rather than a remote host) and §8.

## 7. Overall Phase 2 status

| WIN-INT item | Status |
|---|---|
| L2: lock release after a hard kill | PASS (Phase 2 report §10) |
| L3: path aliases (case, junction, **subst**) | **PASS** |
| L6: non-local drive refusal (stub + **real SMB share**) | **PASS**, loopback SMB (§4.4) |
| R8: suspend/resume (simulated + **real Modern Standby and Hibernate**) | **PASS** |

**Phase 2 Windows validation: complete on this machine.** L6 carries the scope note that the share was
loopback, which the user accepted before the test. The Phase 2 report's verdict, "PASS WITH PENDING
WINDOWS TESTS", no longer has pending WIN-INT items.

The operator backup in Phase 2 report §11 is still required before the first real start of the Phase 2
build. It is not a WIN-INT item and was not done here.

## 8. Remaining operational limitations

1. **Machine.** The tests ran on one non-dedicated laptop (Windows 11 build 26300) without elevation.
   One cycle was run for each suspend type. Lid-close, critical-battery hibernate and S3 (unavailable on
   this firmware) were not tested.
2. **L6.** Loopback SMB only (§4.4).
3. **Detection latency.** A resume is detected at the first heartbeat after wake, up to one heartbeat
   interval (15 s) plus scheduling delay. In that window, background loops in the real app could run
   before `RESUMED_AFTER_SUSPEND` is set. The design accepts this (§7.4). The window was not measured,
   because the exact wake time needs the event log, which was not read.
4. **Stage 1 after resume.** The re-run is requested but was not executed, because it needs a broker
   read. Clearing `RESUMED_AFTER_SUSPEND` after a passing Stage 1 is evidenced only by the simulated
   suite test.
5. **Runner gap.** `r8-sleep` does not check the suspend duration. It printed `PASS` for the 73 s
   hibernate that did not meet its own "≥ 2 minutes" instruction. A future runner change could require
   `gap_wall ≥ 120 s` before passing. It was not changed here, because this task changes no source.
6. **`net use /persistent:no` changes a per-user default.** Anyone re-running L6 with a mapped drive
   should restore it with `net use /persistent:yes`, as was done here.
7. **Placeholders.** The `WIN-INT PENDING` skips in `tests/test_instance_lock.py` (lines 194, 233, 674)
   still read PENDING. Updating them, and the Phase 2 report, to point at this report is a separate,
   approved edit.

## 9. Recommendation on Phase 3 readiness

**Phase 3 readiness: still NO-GO. Phase 3 is not authorized or started.**

The WIN-INT prerequisite (Phase 2 report §13 item 3; U6 mitigation report §9 item 1) is now satisfied,
subject to the L6 loopback note. These prerequisites remain:

1. **U1:** DDPI/TPIN for GTT-triggered CNC sells.
2. The **D§11.2 dark-schema amendments** (Q1–Q3), and renumbering the dark `_m011_*`/`_r011_*` functions
   to **v12** before the journal is registered.
3. **P§10 items 3, 4, 5 and 9**, which are live-trading blockers.
4. Optional, to remove the L6 caveat: re-run L6 against a share on another host (§4.4).

**Live trading: NO-GO.** `LIVE_ENTRIES_ENABLED = False`.

## 10. Final integrity checks

Run after all tests: `integrity.py after`, then `compare.py "2026-11-08 17:55"`. Both are read-only.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged |
| 2 | Worktree inventory | `git status --short` (98) and `--ignored` (149) are **identical** to the baseline, line for line, measured before this report was written. This report adds one `??` entry. |
| 3 | Files changed | A walk of the whole repo (excluding `.git` and `.venv`) found **0 files** with an mtime after the session start (17:55), before this report was written. This includes `backend/kite_terminal.db*` and `.env`, so neither was modified. |
| 4 | `backend/execution.py` | LF sha256 `0ac96482…0bf` and raw `76396481…1e3`, unchanged; `git diff --quiet HEAD -- backend/execution.py` exits 0 |
| 5 | PK-25 frozen hashes | **13 / 13 match**, identical to the baseline |
| 6 | `LIVE_ENTRIES_ENABLED` | `False` |
| 7 | `.env` / account DB / backups | Not opened, read or copied. Every database in this task was a fresh `mkdtemp` scratch file. The Phase 0 guard was installed in every process. |
| 8 | Broker calls | None. No Kite MCP tool or broker API was used. The Stage 1 request was replaced by a recorder, and the broker fence was exercised without a broker object. |
| 9 | Migrations | None on any real database. Migrations 1–11 ran only on scratch databases created by `r8-sleep` / `r8_extended.py` (§1.2). |
| 10 | Source changes | None. The runner, the child helper and all backend files are unchanged. |
| 11 | Staging / commits / pushes / stash | Nothing staged (`git diff --cached --name-only` is empty). No commit, push, merge or stash (`git stash list` is empty). No tag, reset, clean or checkout. |
| 12 | Stray processes | None (`Win32_Process` shows no python/uvicorn) |
| 13 | System state | `subst` and `net use` are empty, and the "remembered connections" default is restored to its baseline value |

**The only new file is this report** (`docs/PHASE_2_WINDOWS_VALIDATION_REPORT.md`).
