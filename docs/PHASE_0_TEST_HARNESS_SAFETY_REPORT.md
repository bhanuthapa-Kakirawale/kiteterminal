# Phase 0: test-harness safety report (finding F-0)

**Scope:** Phase 0 of the GTT-after-fill remediation
(`docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md` §12) and nothing else. Only test-harness
code, FakeKite and documentation changed. No `backend/` or `frontend/` file was modified.

**Not done, by design:**
- no GTT-after-fill engine, operation journal, adoption, fingerprinting, instance lock or fencing;
- no change to Emergency Exit, entry or exit behaviour, or `backend/execution.py`;
- no PK-25 file touched;
- `LIVE_ENTRIES_ENABLED` stays `False`;
- no broker connection or broker API call, no scheduler, no DMB pipeline;
- nothing staged or committed.

The GTT lifecycle proposal itself is unchanged.

---

## 1. Recorded starting state (2026-11-07)

| Item | Value | How established |
|---|---|---|
| Repository | `D:/Claude_WS/kite-terminal` | `git rev-parse --show-toplevel` |
| Branch | `feature/signal-detail-and-strategy-revision` | `git branch --show-current` |
| HEAD | `35a0e07` (expected `35a0e07`) | `git rev-parse --short HEAD` |
| Working tree | 31 tracked files modified, 9 untracked, nothing staged. Identical to the expected snapshot. | `git status --porcelain=v1 --untracked-files=all`, `git diff --cached --name-only` |
| `backend/execution.py` | identical to HEAD | `git diff --quiet HEAD -- backend/execution.py` |
| PK-25 frozen hashes | 13 / 13 match (sha256, CRLF→LF, `research/pk25/validation_manifest.json` → `model_freeze`) | recomputed with a script |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) | grep |
| pytest plugins and site hooks | no pytest-dotenv plugin; no `sitecustomize`; the only `.pth` file is `pywin32.pth` | site-packages listing |
| Protected files | `.env` (195 B) and 41 `backend/kite_terminal.db*` files: the database plus backups, no WAL/SHM present. Also `backend/ui_prefs.json`. | metadata only (`os.stat`): size and mtime saved before any test ran. None of them was opened. |

No pytest run took place before the fix. Running pytest at that point would itself have read the real `.env`.

## 2. The original safety problem

The lifecycle review (§0.1, finding **F-0**) did not run the FakeKite suite because running it
would read the real `.env`:

1. The `env` fixture in `tests/conftest.py` imports `backend.main`.
2. `backend/main.py:5-7` runs `from dotenv import load_dotenv; load_dotenv()` at import time.
3. With no path given, `load_dotenv()` calls `find_dotenv()`. That walks up from `backend/`
   to the repository's real `.env` and loads every key into `os.environ` that is not already set.

## 3. Root cause

- **Import-time side effect.** Importing `backend.main` loads configuration. The test harness
  had no control point before that import.
- **The guard did not cover configuration.** `tests/real_data_guard.py` protected only
  `backend/kite_terminal.db*`. Nothing protected `.env`.
- **Leaked values outlived the test.** `load_dotenv` writes straight into `os.environ`.
  pytest's `monkeypatch` restores only keys it set, so values loaded from the real file stayed
  for the rest of the session. That included the real `KITE_API_KEY`, `KITE_API_SECRET` and
  `KITE_TERMINAL_ENCRYPTION_KEY`, which every later test that does not use `env` would see. The
  existing autouse fixture removed only `KITE_REST_PROXY`.
- **A write path to `.env`.** `crypto.ensure_encryption_key()` appends a newly generated key to
  `crypto.ENV_PATH`, which is the real `.env` by default. A test without the `env` fixture and
  without a key in the environment could have written to it.
- **Inherited shell variables.** Any `KITE_*` variable present in the shell that launched pytest
  reached the tests unchanged.
- **No network boundary.** Nothing stopped a test from reaching the internet. §6.3 shows this
  was really happening.

## 4. Implementation changes

| File | Change |
|---|---|
| `tests/real_data_guard.py` | Extended the existing audit-hook guard (details below). |
| `tests/conftest.py` | Installs the guard, neutralises `dotenv`, scrubs the environment and adds the session fixture, summary and session check (details below). |
| `tests/demo_server.py` | The e2e harness, which runs as a child process outside pytest. It now calls `real_data_guard.install()` and `neutralise_dotenv()` before any `backend` import. Its sentinel values are unchanged. |
| `tests/test_real_data_guard.py` | The one deliberate refusal of the real database path is wrapped in `real_data_guard.expected()`. |
| `tests/test_nse_calendar_monitor.py` | Fixed a real network leak that the new guard exposed (§6.3). |
| `tests/fake_kite.py` | Added opt-in Phase 0 mock scenarios; all default to off (§5.3). |
| `tests/test_harness_isolation.py` (new) | Regression tests C1–C5, network guard tests, demo-server check (§5.1). |
| `tests/test_fake_kite_extensions.py` (new) | Coverage for every new FakeKite scenario (§5.3). |
| `docs/TESTING.md` | §1.3 now describes the extended guard. |

**`tests/real_data_guard.py`:**
- Additional protected files:
  - the repository's `.env`, and any `.env.*` beside it except the committed template `.env.example`;
  - `backend/.env`;
  - `backend/ui_prefs.json`.
- Network check: the `socket.connect` audit event raises `ExternalNetworkAccess` for any address
  other than loopback or a Unix socket.
- Refusal log `(kind, target, decoy, expected)`, with `expected()` and `unexpected_refusals()`.
- `neutralise_dotenv()`: guarded replacements for `load_dotenv` and `dotenv_values`, in both
  `dotenv` and `dotenv.main`:
  - implicit discovery returns `False` / `{}` and opens nothing;
  - an explicit protected path raises `RealAccountDataAccess`;
  - an explicit unprotected path or a stream is passed on to the real loader.
- `scrub_kite_environment()`: removes every `KITE_*` variable and keeps only the names.
- `recording()`: captures `open` and `sqlite3.connect` paths for tests.

**`tests/conftest.py`:**
- At import time, in this order:
  1. installs the guard;
  2. raises if `backend.main` is already imported;
  3. calls `neutralise_dotenv()`;
  4. scrubs the `KITE_*` environment.
- A session-scoped autouse fixture points `crypto.ENV_PATH` and `ui_prefs.PREFS_PATH` at session
  temp files. `db.DB_PATH` is deliberately **not** redirected: a test that forgets `env` must fail
  on the guard, not run silently against some other database.
- `pytest_terminal_summary` prints a "test isolation" section.
- `pytest_sessionfinish` fails the session on any unexpected refusal, including one that
  application code swallowed with a broad `except`.

No production code changed. In particular, `backend/main.py` still calls `load_dotenv()`, and the
production `.env` loading and authentication safeguards are untouched.

## 5. Test coverage

### 5.1 `tests/test_harness_isolation.py`: 40 tests

**C1: test initialisation does not load the real `.env`.**
- `dotenv.load_dotenv`, `dotenv.main.load_dotenv` and `dotenv.dotenv_values` are the guard's
  stubs. `backend.main.load_dotenv`, the name `main.py` bound at import, is the stub, so its
  import-time call was neutralised in this process.
- Implicit `load_dotenv()` and `dotenv_values()` load nothing and open nothing. This holds even
  with a decoy `.env` in the working directory.
- An explicit decoy marked protected is refused before it is opened.
- An explicit temporary file still loads, so delegation is preserved.

**C2: no real Kite credentials.**
- Outside `env`, no `KITE_*` variable exists.
- Inside `env`, only sentinels: `test_api_key`, `test_api_secret`, `http://127.0.0.1:9`.
- `crypto.ENV_PATH` and `ui_prefs.PREFS_PATH` are temporary files both with and without `env`.
- Scrubbing removes `KITE_*` variables, leaves others alone, and keeps names but never values.

**C3: database work uses only the temporary database.** Under `recording()`, every
`sqlite3.connect` from a direct connection and from `/api/tickets` and `/api/orders` targets
`env.tmp_path`. No protected path appears.

**C4: protected paths are refused.**
- The predicate is checked on path strings for `.env`, `.env.local`, `.env.production`,
  `.env.bak`, `backend/.env` and `ui_prefs.json`, including upper-case forms on Windows.
- `.env.example`, `docs/.env`, `tests/.env` and a temporary `.env` are not protected.
- read, append, `read_text` and `os.open` on a protected decoy `.env` are all refused, and the
  file is unchanged afterwards. Append is the channel `ensure_encryption_key` uses.
- `db.get_connection()` on the real database path is refused and logged as expected.
- No unexpected refusal has been recorded.

**C5: importing the app has no unintended side effects.** A child Python process (no `KITE_*` in
its environment, cwd at the repository) installs the guard and stub, records audit events and
imports `backend.main`. Results:
- no `.env*` opened;
- no database or `ui_prefs.json` opened;
- **zero `sqlite3.connect`**;
- no guard refusal;
- no `KITE_*` variable afterwards;
- no logging `FileHandler`;
- the stub recorded exactly one call: `load_dotenv`, implicit, neutralised.

  One loopback `socket.connect` is allowed and asserted to be loopback (§7, item 4).

**Network guard.**
- A connect to TEST-NET-1 (`192.0.2.1`) is refused before it happens and logged.
- Loopback is not refused.
- Address classification is tested, including hostnames such as `api.kite.trade`, which is refused.

**Demo server check.** `tests/demo_server.py` installs the guard and neutralises `dotenv` before
its first `backend` import and before importing FakeKite.

### 5.2 `tests/test_real_data_guard.py`: 24 tests (existing, one test adjusted)

The guard's existing database protection tests all still pass.

### 5.3 FakeKite: `tests/test_fake_kite_extensions.py`, 15 tests

All scenarios are **opt-in, off by default**, and labelled in code as configurable mocks rather
than verified Zerodha behaviour. "UNVERIFIED" marks the proposal's §10 unknowns. The additions
sit on top of the user's uncommitted FakeKite edits (circuit map, `positions_from_fills`, `quote`,
exchange-aware holdings), which are preserved.

| Capability | Proposal ref. | Note |
|---|---|---|
| `SimulatedCrash(BaseException)` with the existing `raise_on_next` / `accept_then_raise` | §9.2.1 | not absorbed by `except Exception`; models a crash before or after the broker took effect |
| `gtt_list_lag_calls`: a new GTT is missing from the next N `get_gtts()` listings | §9.2.4 | UNVERIFIED read-after-write behaviour |
| `gtt_timestamps`: `created_at` / `updated_at` on GTTs; modify and delete bump `updated_at` | §9.2.3 | UNVERIFIED shape |
| `snapshot_orders()` / `serve_orders_snapshot()`: stale or duplicate `orders()` | §9.2.10 | served once each, in order |
| `fail_every_call(exc)` / `expire_session()` (`TokenException`); a proxy outage via `requests.ConnectionError` | §9.2.8 | the unrecorded calls `profile`, `trades`, `mf_holdings` and `instruments` are unaffected (documented) |
| `order_history_previous_days = "raise"` | §9.2.7 | UNVERIFIED |
| `trigger_gtt(status_message=…)`; `trigger_gtt_against_holdings(policy="reject" / "partial" / "oversell")` | §9.2.5 | UNVERIFIED; all three outcomes selectable |

A further test confirms that with every default left alone, existing behaviour is identical.

**Deferred** to the phase that first needs each, as agreed: day rollover (§9.2.6), the
shared-state multi-process broker (§9.2.2), and the `os._exit` subprocess crash harness (§9.2.1).

## 6. Verification evidence

### 6.1 Commands executed

All commands ran from `D:/Claude_WS/kite-terminal` with `.venv/Scripts/python.exe`.

1. Narrow run, first, to prove the stub and guard were active before anything broader:
   `python -m pytest tests/test_harness_isolation.py tests/test_real_data_guard.py tests/test_fake_kite_extensions.py -rA`
   - First attempt: 75 passed, 4 failed. All four were faults in my new tests:
     - a leaking sentinel caused by `monkeypatch.delenv` semantics;
     - a timestamp test that assumed sub-second resolution;
     - the summary line counted names from the scrub test;
     - the loopback socket described in §7, item 4.
   - After fixing the tests: **79 passed, 0 failed.** Isolation summary: implicit loads
     neutralised 4, unexpected refusals 0.
2. Full suite, run 1:
   `python -m pytest -rs --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py`
   - **3084 passed, 1 failed, 3 skipped** (600.95 s).
   - The failure was `test_nse_calendar_monitor.py::test_the_cli_prints_and_exits_with_the_status`.
     The network guard refused two connections to an NSE host on port 443 (§6.3).
3. `python -m pytest tests/test_nse_calendar_monitor.py -q` after the fix: 10 passed, 0 refusals.
4. Full suite, run 2 (final), same command as run 2: **3085 passed, 0 failed, 3 skipped** (569.68 s), exit code 0.
   Isolation summary:
   ```
   python-dotenv neutralised: True; implicit loads neutralised: 4; explicit loads delegated (temporary files): 4; explicit protected loads refused: 2
   inherited KITE_* variables scrubbed at start: 0
   guard refusals: 18 (decoy 15, expected 3, unexpected 0); network refusals: 1
   ```
   The single network refusal is the deliberate TEST-NET-1 test. The 3 expected refusals are the
   two deliberate real-DB-path tests and that network test.

The two `--ignore`d files start the Daily Market Byte interpreter from a sibling checkout as a
child process. That process is outside this guard. Both files were excluded under the "no DMB
pipeline" rule and were **not executed**. The 3 skips are the existing `test_dmb_inbox.py`
symlink tests: Windows does not allow creating symlinks without the required privilege.

### 6.2 Was the full suite safe to run?

Yes, once step 1 had passed:
- the stub, the guard, the environment scrub and the session temp paths are all in place before
  any test module is collected;
- `conftest.py` raises if `backend.main` was imported first.

The full suite was not run at any point before these were in place.

### 6.3 A real isolation leak found and fixed

`backend/nse_calendar_monitor.check(year, *, fetch=_fetch)` binds its default when the function is
defined. The CLI test patched `mon._fetch`, which the CLI path never uses, so **this test made a
live HTTPS request to nseindia.com on every run**. It passed only because NSE's live answer
happened to yield the expected status.

Under the network guard, the connection was refused, the test failed, and the session was marked
failed. Fix, test-only: the test also patches `mon.check.__kwdefaults__["fetch"]`, restored by
`monkeypatch`. The host was NSE, not Kite, so no broker was involved. Production code is unchanged.

### 6.4 Does importing `backend.main` still cause side effects?

- `backend/main.py` is unchanged and still calls `load_dotenv()` at import. In production that is
  intended behaviour.
- **Under the harness**, the call reaches the stub and loads nothing (C5: exactly one implicit,
  neutralised call; zero `.env` opens).
- Importing `backend.main` opens **no database** (zero `sqlite3.connect`) and creates no log file.
  Database initialisation happens only in the `startup` hook, which tests never run.

### 6.5 Evidence that the real `.env` was not read

- Every test session reports `python-dotenv neutralised: True`. Implicit loads were all neutralised.
- **Zero unexpected guard refusals** in the final full run (`guard refusals: 18 (decoy 15, expected 3, unexpected 0)`). Any
  `open`/`sqlite3.connect`/`copyfile` of the real `.env` would have been refused before the file
  was opened, and recorded. The 2 `explicit protected loads refused` and all decoy refusals came
  from decoy files in `tmp_path`.
- C5 shows that a fresh import of the app opens no `.env*` file.
- `.env` size and mtime are unchanged before and after (§8).
- Limitation: NTFS last-access times are usually not maintained, so "not read" rests on the guard
  log, the stub record and the C5 child process, not on atime.
- At no point in this phase did I (the agent) open, read, copy or print `.env`. Only its existence,
  size and mtime were observed.

### 6.6 Evidence that the real account database was not opened or modified

- The guard refuses every channel to `backend/kite_terminal.db*` and logs each refusal. In the
  final run, the only real-path refusals were the deliberate, `expected()` tests, and none was
  unexpected.
- C3 shows `env` database traffic stays in `tmp_path`. C5 shows importing the app makes no SQLite
  connection.
- Size and mtime of all 41 `kite_terminal.db*` files and `ui_prefs.json` are unchanged (§8).

## 7. Remaining risks and limitations

1. **Child processes are outside the audit hook.** The suite's own children are:
   - `node` (UI harnesses);
   - `git check-ignore`;
   - the C5 child (installs the guard itself);
   - `demo_server.py` (installs the guard itself).

   The excluded DMB tests start DMB's interpreter in its own repository. That interpreter could
   load DMB's own configuration; it is not protected by this guard.
2. **`tests/e2e_*.py` and `tests/acceptance_dmb_kite.py`** (Playwright and a real demo server)
   were not run in this phase. `demo_server.py`'s guard installation is checked statically, not by
   starting the server.
3. **The network guard covers `socket.connect` only.** DNS lookups (`getaddrinfo`) are not
   blocked, and a hostname is resolved by the OS before the connect is refused. UDP `sendto`
   without connect is not covered.
4. **Twisted loopback waker.** Importing `kiteconnect` (KiteTicker → Twisted reactor) connects a
   socket to Twisted's own `127.0.0.1` listener (a self-pipe). It is loopback and allowed, and C5
   asserts at most one such connect, to loopback only.
5. **`.env` in parent directories.** `find_dotenv()` could find a `.env` above the repository. That
   is moot while implicit discovery is neutralised, and an explicit path to one would still load
   (not protected).
6. **`logs/`** (the launcher's rotating log) is not a protected path. Tests never set
   `KITE_TERMINAL_LOG_FILE` before `backend.main` is imported, and inherited values are scrubbed.
   Tests of `backend.server.main()` set and restore it with `monkeypatch`, with uvicorn stubbed.
7. **Process-global state.** The guard is in-process. A future test that deliberately replaces
   `dotenv.load_dotenv` with something that reads a real path would still hit the audit hook, but
   a test that edits `real_data_guard`'s own state could weaken it. The test files are reviewed
   statically.
8. **The FakeKite scenarios are models, not evidence.** Every UNVERIFIED scenario stands for an
   open question in proposal §10, which still needs the separately authorised broker validation
   (Phase 9).

## 8. Final integrity checks

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07`, unchanged |
| 2 | Working tree before / after | Before: 31 modified + 9 untracked (the pre-existing work). After: the same, plus 6 newly modified (`docs/TESTING.md`, `tests/conftest.py`, `tests/demo_server.py`, `tests/real_data_guard.py`, `tests/test_nse_calendar_monitor.py`, `tests/test_real_data_guard.py`) and 3 new (`docs/PHASE_0_TEST_HARNESS_SAFETY_REPORT.md`, `tests/test_fake_kite_extensions.py`, `tests/test_harness_isolation.py`). `tests/fake_kite.py` was already modified; Phase 0 added to it without removing any of the user's changes. The status lines for `backend/` and `frontend/` are identical before and after. |
| 3 | Changed / created files | as in row 2; no `backend/` or `frontend/` file was edited |
| 4 | `backend/execution.py` | byte-identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) |
| 5 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`) |
| 6 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) |
| 7 | Broker operations | none: tests use FakeKite only; the network guard refused every non-loopback connect (only the deliberate TEST-NET-1 test in the final run); no Kite MCP tool or broker API was used |
| 8 | Real `.env` / account database | not read, copied or modified: 0 unexpected refusals; size and mtime of `.env`, all 41 `kite_terminal.db*` files and `ui_prefs.json` are identical before and after (43 files, `os.stat` only) |
| 9 | Staged / committed | nothing staged (`git diff --cached --name-only` is empty), no commit |

## 9. Recommendations, reported separately (the proposal is not modified)

- **Optional production hardening, for a later reviewed phase.** Move `load_dotenv()` in
  `backend/main.py` from import time to the launcher (`backend.server.main`). It already calls
  `load_dotenv()` itself. That would remove the side effect at its source, but it changes startup
  behaviour for `uvicorn backend.main:app` users. It needs its own review, so it was not done here.
- **Proposal §11 / §12, Phase 0 row:** Phase 0 as delivered omits the multi-process harness and the
  subprocess crash harness, which are deferred. Their first consumer, Phase 2 (the instance lock),
  should add them.
- **Proposal §9.1:** the full FakeKite suite now runs. The "suite not run" caveat no longer applies
  from this phase onwards.

## 10. Explicit confirmation

- No GTT-after-fill lifecycle code was implemented.
- No order, entry, exit, Emergency Exit or protection behaviour changed. No `backend/` or
  `frontend/` file was modified.
- `backend/execution.py` and every PK-25 file are byte-identical. `LIVE_ENTRIES_ENABLED` is `False`.
- No broker operation of any kind took place.
- The real `.env` and the account database were not read, copied or modified.
- Nothing was staged or committed.
