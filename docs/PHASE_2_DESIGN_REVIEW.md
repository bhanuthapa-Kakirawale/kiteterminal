# Phase 2 design review: cross-process coordination and startup restriction

**Status: READ-ONLY DESIGN REVIEW. Nothing is implemented, nothing is enabled.**
This document changes no code, test, configuration, schema or database. It does not authorise
Phase 2 implementation, any migration, any broker operation or live trading.
`backend/live_entry_gate.LIVE_ENTRIES_ENABLED` **stays `False`**.

It builds on, and must be read with:

- `docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md` ("the proposal"; cited as P§n);
- `docs/PHASE_0_TEST_HARNESS_SAFETY_REPORT.md` ("Phase 0");
- `docs/PHASE_1_DARK_SCHEMA_REPORT.md` ("Phase 1"; its open questions are P1-Q1 … P1-Q10).

Where this review departs from the proposal or from the Phase 1 DDL, the difference is called out and
listed in §11.

### Labels

Every statement carries one label.

| Label | Meaning |
|---|---|
| **[V]** | Verified from repository: read in the source or docs in this session, with a file reference |
| **[P]** | Proposed design: a recommendation, not implemented |
| **[I]** | Inference: reasoned, not proven |
| **[W]** | Inference that **requires controlled Windows integration verification** |
| **[B]** | Requires broker verification: official Zerodha documentation, or a separately authorised read-only or supervised check |

---

## 0. Recorded starting state (2026-11-07)

| Item | Value | How established |
|---|---|---|
| Repository | `D:/Claude_WS/kite-terminal` | `pwd` [V] |
| Branch | `feature/signal-detail-and-strategy-revision` | `git rev-parse --abbrev-ref HEAD` [V] |
| HEAD | `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (expected `35a0e07`) | `git rev-parse HEAD` [V] |
| Working tree | 38 tracked files modified, 14 untracked, nothing staged (52 entries). With ignored entries, `git status --porcelain=v1 --ignored` prints 103 lines. Saved to the scratchpad as `status_before.txt`. This is exactly the Phase 1 §12 after-state. | `git status` [V] |
| Phase 1 | §12.4 records **PHASE 1 PASS** (3226 passed, 0 failed, 3 known skips) | read [V] |
| `backend/execution.py` | identical to HEAD | `git diff --quiet HEAD -- backend/execution.py` [V] |
| PK-25 frozen hashes | **13 / 13 match**: 11 `production_file_sha256` + 2 `model_file_sha256`, sha256 with CRLF→LF, from `research/pk25/validation_manifest.json` → `model_freeze` | scratchpad script [V] |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) | grep [V] |
| SQLite / Python | 3.50.4 / 3.13.14 | `sqlite3.sqlite_version` [V] |

What was done: I read the repository, the canonical documents and the source of the modules listed
in §2 [V].

What was not done:

- no test run;
- no broker contact and no Kite MCP tool;
- no `.env` or account database access (only `git status`, which lists names, touched them);
- no scheduler or DMB pipeline;
- nothing staged or committed.

The only file written is this report [V].

---

## 1. Executive summary

**What Phase 2 must solve.** Today the terminal relies on one rule: "run one worker in one
process" (`server.py:122-135`, `DEPLOYMENT.md:304-321`). Nothing enforces it across processes
[V]:

- a second `python -m backend.server` on another port runs a second order monitor, trailing
  engine and reconciliation against the same SQLite file;
- so would a pre-Phase-2 build, or a CLI;
- the only mutual exclusion around GTT writes is an in-process `threading.Lock`
  (`position_quantity.py:217-223`);
- the background loops start immediately at startup, with no reconciliation gate
  (`main.py:107-150`).

Broker writes happen in **both** worker loops and HTTP request threads: ticket confirmation,
Emergency Exit, manual cancels [V].

**Recommended architecture [P].** Three independent layers, each needed and none sufficient
alone:

1. **OS exclusive lock** on a dedicated file `<DB_PATH>.instance.lock`, taken before `init_db`.
   This decides *which local process may operate*. The kernel picks an atomic winner and releases
   the lock when the process dies, so PID reuse is irrelevant to liveness.
2. **Database fencing epoch** in a singleton `app_instance` row, incremented under `BEGIN IMMEDIATE`
   after the lock is held. It is checked inside every Phase 2+ coordination transaction and by a
   heartbeat. This decides *whether a process may still write coordination state*. It catches a
   lock bypass (network or synced folder, a path the lock does not cover, an older build).
3. **Process-level write authority.** One in-memory flag gates three things:
   - a write-fence at the single SDK choke point (`KiteConnect._request`, `kite_client.py:81-157`),
     which covers the frozen `execution.py` and `strategy_engine.py:677` without editing them;
   - the write routes;
   - the loops.

   A process that loses authority stops sending new broker writes.

**What fencing cannot do.** A database token cannot recall a broker request that has already left
the process [I]. The design handles this the way the proposal does:

- a write-ahead `sending` row, committed before the request;
- never re-sending a create;
- settle windows counted in complete reads;
- successor reconciliation that treats a predecessor's `sending` row as `unknown`.

**Startup [P].** Lock → `init_db` → epoch takeover → persist
`RESTRICTED(STARTUP_RECONCILIATION_INCOMPLETE)` → start loops → **Stage 1, read-only
reconciliation**:

- local inventory;
- session and proxy checks;
- `orders`, `get_gtts`, `holdings`, `positions`, each with its timestamp and error;
- attribution and classification;
- the outcome persisted.

**Stage 2 (recovery writes) is deliberately empty in Phase 2**, because no protection journal is
live yet. Phase 2 builds the gate, not the writes. New entries stay blocked until a complete
Stage 1 passes and every restriction reason is clear. Exits, cancellations, Emergency Exit and
today's protection upkeep are never blocked by RESTRICTED.

**The four open questions are resolved (§3, §4):**

- **Q1:** `superseded` stops being a state. It becomes an overlay on an operation whose broker
  outcome may still be unknown.
- **Q2:** `abandoned` is split into `not_sent` (provably nothing sent; an automatic new create is
  allowed) and `absent_after_settle` (sent; absence is inference; never retried automatically; a
  late appearance is watched for).
- **Q3:** `needs_review` stays in the open set and blocks automated protection writes. It does not
  block an exit claim: the exit claim supersedes open operations in the same transaction. The open
  index is partitioned by kind, so a cancel can coexist with an unresolved create or modify.
  Clearing needs an audited human resolution.
- **Q4:** concrete DDL for `app_instance`, `app_instance_history`, `instrument_attribution`,
  `broker_reads` and `broker_quantity_observations`. The attribution key is
  exchange + tradingsymbol (+ token), **not** ISIN, because positions, orders and the instrument
  cache carry no ISIN (§2.6). This is an amendment to P§6.2.

**Schema implications [P].**

- Phase 2 needs a **registered** forward-only migration (v11). It changes the real database on the
  next start and makes older builds refuse it. That is a governance decision for the
  implementation authorisation (§11.1).
- The Phase 1 dark schema needs amendments for Q1–Q3. They cost nothing now, because the table is
  unregistered and empty everywhere (§11.2).

**Decision: PHASE 2 DESIGN READY (§14)**, with the preconditions stated there. Live trading stays
**NO-GO**. DDPI/TPIN for GTT-triggered CNC sells (P§10 item 6) remains a live-trading blocker.

---

## 2. Current architecture findings (all [V] unless labelled otherwise)

### 2.1 Process start, loops and shutdown

| Finding | Reference |
|---|---|
| `backend/main.py` calls `load_dotenv()` at import (`:5-7`). The launcher `python -m backend.server` calls it again (`server.py:271-273`). | [V] |
| `on_startup` order: `enforce_runtime_policy()` → `init_db()` → sector seed → ticker start and instrument refresh (if a session exists) → `workers.supervisor.start(_BACKGROUND_LOOPS)` (5 loops: trailing, data_jobs, reconciliation, order_monitor, automation) → optional startup sync. **No lock, no reconciliation gate.** | `main.py:98-150` [V] |
| `on_shutdown`: ticker stop, then `supervisor.stop(timeout=10 s)`. "A cycle already inside asyncio.to_thread cannot be interrupted mid-call." | `main.py:153-158`, `workers.py:1-17`, `config.py:174` [V] |
| The supervisor prevents duplicate loops **within one process only** (`_alive`, `start`). | `workers.py:40-64` [V] |
| One-worker and loopback policy: `validate()` refuses `workers != 1` and a non-loopback bind without opt-in. `detect_runtime()` reads the launcher's exports or parses a bare uvicorn command line best-effort. | `server.py:122-205` [V] |
| `DEPLOYMENT.md` asks for "no second copy of the terminal against the same database, not even on another port". Documentation only; nothing enforces it. | `DEPLOYMENT.md:304-321` [V] |
| Repository-wide grep: no `msvcrt`, `fcntl`, named mutex, lock file, `app_instance` or `getpid` use. No existing process or file locking. | grep [V] |
| Other entry points with `__main__`: `db_maintenance`, `live_preflight`, `nse_calendar_monitor`, `smart_money_validation`, `trading_calendar`, `volume_research`, `server`. | grep [V] |
| uvicorn runs the lifespan startup **before** binding the listening socket. So a second instance on the *same* port runs `on_startup` (`init_db`, ticker, loops) before failing to bind. | [I]: uvicorn 0.34 behaviour, not re-read in this session |
| With `--dev-reload`, the reloader parent does not import the app; each reload child runs `on_startup` and is terminated on the next reload. | [I] |

### 2.2 Database connections and transactions

| Finding | Reference |
|---|---|
| `get_connection()`: a new `sqlite3.connect(DB_PATH, timeout=10 s)` per use, `PRAGMA busy_timeout=10000`, `journal_mode=WAL`, `foreign_keys=ON`. Python's default isolation level, so an implicit **deferred** `BEGIN` before DML. | `db.py:468-477`, `config.py:156` [V] |
| The ticket compare-and-set `_transition` is a single `UPDATE … WHERE status IN (…)`. Atomic per statement; no `BEGIN IMMEDIATE`. | `routes/tickets.py:48-69` [V] |
| Explicit `BEGIN IMMEDIATE` appears only in `migrate()` (`db.py:2214`), the Phase 1 dark step (`db.py:2524`), `dmb_inbox.py:245` and `ohlc.py:211`. | grep [V] |
| In WAL mode, a *deferred* read transaction that later tries to write can get `SQLITE_BUSY` (snapshot conflict) **without** the busy handler waiting, if another connection committed meanwhile. `BEGIN IMMEDIATE` takes the write lock up front and waits under `busy_timeout`. | [I]: documented SQLite behaviour; the basis for using `BEGIN IMMEDIATE` for every fenced transaction (§7.3) |
| `migrate()` is forward-only. It backs up the file (`pre_schema_v<N>_bak`, verified) and applies each pending entry of `MIGRATIONS` in its own `BEGIN IMMEDIATE`. `_check_history` refuses a database from newer code (`DATABASE_SCHEMA_UNSUPPORTED`). `LATEST_SCHEMA_VERSION = 10`. | `db.py:2078-2242` [V] |
| `init_db()` first calls `crypto.ensure_encryption_key()`, which can append to `.env`, then migrates, runs startup invariants and backfills. | `db.py:2262-2284` [V] |

### 2.3 Phase 1 dark schema (as implemented)

| Finding | Reference |
|---|---|
| States: `intent, sending, acknowledged, confirmed, rejected, unknown, adopted, abandoned, superseded, needs_review`. | `db.py:2311-2314` [V] |
| Open set (`PROTECTION_OPEN_OPERATION_STATES`) = `intent, sending, acknowledged, unknown, needs_review`. `idx_protection_ops_one_open` is unique on `position_id` over that set, for all kinds together. | `db.py:2317`, `:2373-2376` [V] |
| CHECKs: `intent ⇒ sent_at IS NULL`; every sent state (`sending, acknowledged, confirmed, rejected, unknown, adopted`) ⇒ `sent_at NOT NULL`; `abandoned`, `superseded` and `needs_review` impose no `sent_at` rule; `kind='create' ⇒ attempts ≤ 1`; modify/cancel ⇒ `target_gtt_id NOT NULL`; an acknowledged, confirmed or adopted create ⇒ `result_gtt_id NOT NULL`. | `db.py:2342-2372` [V] |
| Not registered in `MIGRATIONS`. Applied only by explicit calls that refuse the account database. No module reads or writes it. | `db.py:2287-2309`, Phase 1 §6.3 [V] |
| Not created: `app_instance`, `instrument_attribution`, `broker_gtt_observations` (decision D2). | Phase 1 §3 [V] |

### 2.4 Broker-write sites and the SDK choke point

| Finding | Reference |
|---|---|
| Every real KiteConnect is built in `create_kite_connect()`. Direct route: narrowed to `DirectRouteKiteConnect` (writes refused). Writes disabled: narrowed to `ReadOnlyKiteConnect`. Both override `_request`, the SDK's single request method, and refuse every non-GET except `api.token`. | `kite_client.py:54-157` [V] |
| `_narrow()` changes only a **real** `KiteConnect`. FakeKite and the paper broker keep their own methods. | `kite_client.py:101-106` [V] |
| Write call sites: `execution._execute_live` / `_execute_exit` (**frozen**); `strategy_engine.py:677` `modify_gtt` (**frozen**, PK-25 production file); `trailing_engine.py:811`; `position_quantity.sync_protection` / `remove_protective_gtt`; `gtt_cancellation.cancel` (`:58`); `routes/orders.py:132-134` (`cancel_order`) and `cancel_gtt` (`:165`); `routes/kill_switch._cancel_unfilled_entry` (`:75`) and `_refresh_entry_fill` → `sync_protection` (`:447-457`); `routes/tickets.confirm_and_execute_ticket` → `execution.execute_ticket`. | grep [V] |
| These run in worker threads (`asyncio.to_thread` in the loops) **and** in FastAPI sync route handlers, which run in the threadpool. | `reconciliation.py:90-103`, `kill_switch.py:89-142` [V] |
| `kiteconnect` 5.0.1: `place_gtt` has no tag or idempotency key; default timeout 7 s; no retry. | P§5.1 [V, cited from the proposal's code read] |
| The REST proxy (NET-1) is mandatory for writes. Whether the proxy itself can retry or replay a non-idempotent request is not established here. | [B]: proxy configuration review |

### 2.5 Ticket and position lifecycle, existing reconciliation

| Finding | Reference |
|---|---|
| Entry claim, gates, `_record_execution_result` and the late result, as tabulated in P§2.1. | [V] |
| The exit claim predicate `_exit_claim_condition` (the position is open and holds exactly the exit quantity) is evaluated in the claiming UPDATE. `idx_trade_tickets_one_exit_owner` admits one in-flight exit per position. | `routes/tickets.py:819-836`, `db.py:964-998` [V] |
| `account_safety_state` holds the persisted per-account lock and breaker (`trading_locked`, `breaker_open`, …). There is no restriction state or startup state yet. | `db.py:382-397` [V] |
| `final_gate` runs, in order: trading lock, ticket review, the hard live gate, REST route, breaker, live armed for the session, notional, regular market only, daily loss, `check_live_critical_state` (via `live_preflight.confirm_time_blockers`), LTP and price sanity, presellable guard, margin preflight. | `account_safety.py:935-999`, `:541-576` [V] |
| `reconciliation._run_cycle` (every 180 s): one `BrokerSnapshot` per cycle; **a failed read is `None`, never an empty list** (`:117-164`). Each check runs in its own savepoint; a `TokenException` aborts the cycle. Cross-cycle memory (`_reverse_seen`, `_price_seen`, `last_cycle`) is process memory. | `reconciliation.py:64-222` [V] |
| `_check_position_state` reports only a **shortfall**, by exchange+symbol: "holding more … is normal when the stock is also held outside the app". | `reconciliation.py:746-801` [V] |
| `gtt_observations`: process memory only. After a restart everything is UNVERIFIED. PROTECTED needs an observation no older than `GTT_OBSERVATION_MAX_AGE_SECONDS` (420 s). | `gtt_observations.py:1-17`, `config.py:98` [V] |
| `gtt_cancellation.cancel`: one delete, then a mandatory read-back; `CONFIRMED` / `FAILED` / `OUTCOME_UNKNOWN`; never retried. | `gtt_cancellation.py:1-97` [V] |
| `order_monitor` resolves unknown submissions by tag. `SUBMISSION_STALE_SECONDS = 120`. | `order_monitor.py:140-155, 339-451`, `config.py:27` [V] |

### 2.6 Instrument identity

| Finding | Reference |
|---|---|
| `broker_holdings.instrument_quantities` keys on (EXCHANGE, TRADINGSYMBOL), keeps the instrument token, and says explicitly that it "has no ISIN for the app's positions". Per key: settled + T1 + day CNC net; fails closed per key on duplicates, conflicting tokens, unreadable or negative values. | `broker_holdings.py:1-151` [V] |
| `instrument_cache` has **no ISIN column**. | `db.py:129-138` [V] |
| `holdings_snapshot` has an `isin` column. `live_entry_gate._holdings_matches` uses `holdings[].isin` when present, to find the same holding under another exchange. | `db.py:38-47`, `live_entry_gate.py:138-157` [V] |
| FakeKite holdings carry no ISIN. | `tests/fake_kite.py` grep [V] |
| Kite holdings rows carry `isin`; positions and orders rows do not; the instruments dump has no ISIN. | [B] (the repository *assumes* holdings may carry it) |

### 2.7 Emergency Exit (working tree, E3)

| Finding | Reference |
|---|---|
| Runs in an HTTP request thread. One `_broker_snapshot` per click (holdings, positions, orders). Refuses an instrument on an external SELL today, a shortfall, inconsistent figures, a token mismatch or an unreadable snapshot. Sells exactly the app's open quantity. | `kill_switch.py:89-357` [V] |
| An unfilled entry is cancelled first; `_refresh_entry_fill` then records the fill and calls `sync_protection` (a GTT modify). | `kill_switch.py:385-457` [V] |
| Never blocked by an in-flight protection write today, because no protection journal exists. | [V] |

### 2.8 Test isolation and FakeKite

| Finding | Reference |
|---|---|
| The `env` fixture redirects `db.DB_PATH` to `tmp_path/test.db`, monkeypatches `kite_client.get_kite` to return a FakeKite, and builds a TestClient **without running startup hooks**. | `tests/conftest.py:365-429` [V] |
| The guard protects any path in `backend/` whose name starts with `kite_terminal.db`, plus `.env*` (except `.env.example`) and `ui_prefs.json`. It refuses non-loopback `socket.connect`. A **child process is outside the audit hook** and must call `install()` and `neutralise_dotenv()` itself. | `tests/real_data_guard.py:43-49, 59-132` [V] |
| FakeKite already has: `SimulatedCrash(BaseException)`, `raise_on_next`, `accept_then_raise`, `on_call`, `fail_every_call`, `expire_session`, a GTT listing lag, GTT timestamps, stale order snapshots, and trigger-against-holdings policies. | `tests/fake_kite.py`, Phase 0 §5.3 [V] |
| Deferred from Phase 0 to "the phase that first needs each": the shared-state multi-process FakeKite, the `os._exit` subprocess crash harness, and day rollover. | Phase 0 §5.3, §9 [V] |
| `pywin32` is installed in `.venv` but **not** listed in `requirements.txt`. | [V] |
| `.gitignore` does not cover a `*.instance.lock` file. | [V] |

---

## 3. Answers to the four outstanding questions

### Q1. `superseded`: operation state versus Emergency Exit flag

**Two different concepts [I].**

- The Phase 1 state column answers: *what do we know about the broker outcome of this request?*
- The P§7.3 Emergency Exit "superseded" answers: *does the position still want this request's
  result?*

The first is about broker facts; the second is about local intent. A request can be unwanted and
still have an unknown outcome. That is exactly the dangerous case: the GTT may land after the
position is gone.

**Making `superseded` a terminal state is unsafe [I].** It would remove the operation from the
open-operation index while its broker outcome is unresolved. A second operation could then be
claimed on the position, and the resolution duty (cancel any GTT that lands) would have no open row
to hang on. This confirms P1-Q1.

**Recommendation [P]:**

1. **Remove `superseded` from the state enum.** Represent supersession as an **overlay** on the
   operation row: `superseded_at`, `superseded_by_kind` (`emergency_exit`, `exit`, `close`,
   `user`), `superseded_by_ref` (the exit ticket id or system event).
2. An operation leaves the open set **only when its broker outcome is resolved** (§4). Supersession
   never resolves anything. It changes what the resolution must do.
3. **Atomic pre-send supersession.** The `intent → sending` compare-and-set has two predicates:
   `superseded_at IS NULL` and `NOT EXISTS (exit in flight)`. So:
   - if the supersede commits first, the op can never be sent and becomes `not_sent`;
   - if the `sending` commit wins, the request is treated as in flight and the overlay applies to
     its resolution.

   There is no window in which a superseded op is first sent.
4. **Resolution rule for a superseded op.**
   - The outcome yields or reveals a GTT id (acknowledged, adopted, a late ack, or a modify that
     landed): the resolution transaction links the id, which records the broker fact, and claims a
     `cancel` operation in the same transaction. The cancel is exposure-reducing and automatically
     retried. The position stays `CLOSED_CLEANUP_REQUIRED` (or protection `CANCEL_PENDING`) until
     the cancel is confirmed.
   - The outcome is `rejected`, `not_sent` or `absent_after_settle`: nothing to cancel. An
     `absent_after_settle` op stays under the late-appearance watch (Q2).
5. **Traceability.** The op row keeps `request_payload`, `request_fingerprint`, `sent_at` and
   `instance_epoch`, plus the overlay columns and evidence. An Emergency Exit event (`system_events`)
   names every op it superseded. Reconciliation can always explain a late GTT by the op that may
   have created it.
6. **Can an op be superseded while its broker outcome is unknown?** **Yes, and that is the main
   reason the overlay exists.** The supersede commits immediately and Emergency Exit does not wait.
   Emergency Exit's own decision to sell then follows P§7.3:
   - cancel every *known* linked GTT (one attempt plus read-back);
   - sell only if attribution is `exclusive` and broker quantity equals the app open quantity;
   - otherwise refuse that instrument and instruct the user to act in Kite.

   An unknown create means "a SELL GTT may exist that the app cannot cancel by id". The same
   exclusive-only rule applies, and a `POSSIBLE_DOUBLE_SELL` watch is armed.
7. **A late broker response** is either:
   - an HTTP response received by the still-running thread after the supersede, which resolves the
     op normally; or
   - a GTT appearing in a later book read, which the recovery or reconciliation matches by
     fingerprint.

   Either way the result is linked, then cancelled under rule 4. It never moves the position back
   to OPEN. Position state is driven by fills and holdings, never by a GTT outcome [P].

The full transition table is in §4.3.

### Q2. `abandoned`: never submitted versus ambiguous outcome

**The two meanings carry materially different risk [I]:**

- "created, never submitted" is *proven* safe to replace;
- "submitted, not visible after settle" is *inferred* absent. A GTT may still exist or appear
  (P§10 item 9: `get_gtts()` read-after-write consistency and completeness are unknown [B]).

**Recommendation [P]:** replace `abandoned` with two terminal states, each with a CHECK.

- `not_sent`, CHECK `sent_at IS NULL`. Provably no request left the process for this op. This is
  provable because `sending` is committed *before* the call (P§5.2 step 6) and `intent` carries no
  `sent_at` [V, existing CHECK].
- `absent_after_settle`, CHECK `sent_at IS NOT NULL AND complete_reads_after >= 2`. Absence is
  inference.

| # | Case | Op state path | Automatic retry? | Remains unresolved? | Manual review? |
|---|---|---|---|---|---|
| 1 | Created, definitely never submitted (pre-write check failed, crash in `intent`, superseded before send) | `intent → not_sent` | **Yes, for a create**: a *new* op may be claimed automatically, unless the op was superseded (the position no longer wants it) | No | No |
| 2 | Submitted and acknowledged | `sending → acknowledged → confirmed` (read-back matches); shape mismatch → `needs_review` | n/a | No | Only on a shape mismatch |
| 3 | Submitted, timed out (any non-definite exception, P§4 J) | `sending → unknown` → recovery | **No** for a create or a resize-up. Modify-down and cancel: yes, after a read-back shows the change not applied | Yes, until resolved by reads | No, unless it escalates (cases 5–7) |
| 4 | Submitted, the app crashed before recording the response | `sending` (dead epoch) → `unknown` at Stage 1 | **No** for a create | Yes | No, unless it escalates |
| 5 | Submitted, no matching GTT after settle | `unknown → absent_after_settle` only after **≥ 2 complete, well-formed book reads ≥ 60 s apart**, the first taken **after** `max(sent_at + SETTLE, recovery start)`, with no fingerprint match **and** no other unlinked active SELL GTT on the instrument | **Never automatic.** A user re-create is a new op, explicitly acknowledged ("a GTT from attempt #n could still appear") | Terminal, but **watched** (row 6) | The user decides on re-create |
| 6 | A matching GTT appears late | (a) While the op is `unknown`: exactly one match, nothing else unlinked → `adopted`. (b) After `absent_after_settle`: the op stays terminal; reconciliation raises **`LATE_GTT_APPEARED`** (URGENT), protection `NEEDS_REVIEW`, new creates refused on the instrument | (a) none needed; (b) **none**: never auto-adopted (a user re-create may exist, giving two GTTs) and never auto-deleted (it might be the user's own GTT) | (b) Yes | (b) **Yes** |
| 7 | Several possible matching GTTs | `unknown → needs_review` | None | Yes | **Yes** (Q3 resolution path) |

Further rules [P]:

- **The settle clock counts complete reads, not wall time.** Days offline cannot turn "never
  looked" into "proved absent" (P§4 K). A failed, partial or malformed read increments nothing.
- **The watch for case 6(b):** every reconciliation cycle compares each unlinked active SELL GTT on
  an instrument with the fingerprints of that instrument's `absent_after_settle` ops from the last
  `LATE_MATCH_WATCH_DAYS` (proposed 30; GTT retention and expiry are [B], P§10 items 11–12).
- **No assumption is made about Kite timeout semantics [B].** A `TokenException` on `place_gtt`
  counts as ambiguous, as in P§4 J.

### Q3. `needs_review` and the open-operation index

**The conflict, verified [V]:**

- the index (`db.py:2373-2376`) includes `needs_review`;
- P§7.2's exit-claim predicate lists only `('intent','sending','acknowledged','unknown')`;
- P§7.3 says Emergency Exit is "never blocked by a protection operation".

**Answers [P]:**

| Question | Answer | Why |
|---|---|---|
| Does `needs_review` block a new protection operation? | **Yes**, every automated create and modify on the position. | It means GTT ownership is ambiguous (several matches, an unlinked SELL GTT, a wrong shape). Any further automated write could double-protect or double-sell [I]. |
| Does it block an exit claim? | **No, not in the database predicate.** The exit claim transaction supersedes open ops (Q1). The exit *policy* (Phase 6) then decides how to sell: a strategy or automated exit is **refused** while any op on the position is `unknown` or `needs_review`; a manual exit or Emergency Exit sells only under the exclusive-attribution rule, or after an explicit user acknowledgement that names the candidate GTTs to delete in Kite. | Exits reduce risk, and the user must always be able to exit (P§3.5). The double-sell risk comes from unknown armed GTTs, and blocking the exit does not remove it. |
| Can Emergency Exit supersede or take ownership of an unresolved op? | **Supersede: yes. Take ownership: no.** It sets the overlay and claims cancels only for *known* GTT ids. It never adopts, links or resolves an ambiguous op; only the recovery engine (by reads) or a human (by a resolution record) does. | Emergency Exit is time-critical and must not become a second resolver. |
| How is an unresolved op cleared safely? | `unknown` clears only by reads (adopt / confirm / settle). `needs_review` clears **only** through a human resolution action. It requires a **fresh complete GTT book read** taken at resolution time, which must support the chosen outcome: a GTT linked by the user, "confirmed no app GTT exists", or "deleted in Kite, verified absent". | A human statement without fresh evidence is not enough [P]. |
| Must a manual resolution be auditable? | **Yes.** A new `protection_operation_resolutions` row records op id, outcome, required reason text, evidence read id, resolver, epoch and timestamp. The op moves to a new terminal state **`resolved_manual`** with that row's id. The existing `reconciliation_reviewed` pattern is the precedent. | Traceability (P§3.4). |

**Exact recommended open-state set [P]:**
`('intent', 'sending', 'acknowledged', 'unknown', 'needs_review')`. This is the same set as
Phase 1, so the *set* does not change.

**What changes is the index shape.** It is partitioned by kind into two partial unique indexes:

```sql
-- at most one open exposure-changing op (create or modify) per position
CREATE UNIQUE INDEX idx_protection_ops_one_open_change ON protection_operations(position_id)
  WHERE kind IN ('create','modify') AND state IN ('intent','sending','acknowledged','unknown','needs_review');
-- at most one open cancel per position
CREATE UNIQUE INDEX idx_protection_ops_one_open_cancel ON protection_operations(position_id)
  WHERE kind = 'cancel' AND state IN ('intent','sending','acknowledged','unknown','needs_review');
```

Why the partition is needed [I]:

- With the Phase 1 single index, Emergency Exit cannot claim a cancel of a *known* GTT while a
  modify or create on the same position is unresolved.
- A delete concurrent with an in-flight modify of the same GTT converges to "gone" in either
  order.
- A cancel cannot target an unknown create's GTT, because `target_gtt_id` is required (existing
  CHECK). That GTT is cancelled by the create's resolution instead (Q1 rule 4).

Claim predicates [P]:

- a **create or modify** claim additionally requires: no open cancel on the position; no exit in
  flight; `superseded_at IS NULL` on any open op of the position; attribution not `broken` or
  `unknown`; epoch current;
- a **cancel** claim requires only the epoch and its own partition.

This is a Phase 1 schema amendment (§11.2). It is not applied silently.

### Q4. Concrete `app_instance` and `instrument_attribution`

The full definitions are in §5 and §6. Their essential choices [P]:

- **`app_instance`** is a singleton row (`id = 1`) holding the current fencing `epoch` and the
  owner's identity, heartbeat and shutdown status. `app_instance_history` keeps one row per start
  (unique epoch).
  - Liveness is decided by the **OS lock**, never by PID or heartbeat. PID reuse cannot fake an
    owner.
  - The heartbeat and lease detect a *lock bypass* and a suspend or resume.
- **`instrument_attribution`** is keyed on `(account_id, exchange, tradingsymbol)` with the
  instrument token checked. ISIN is an *observed linking attribute* (from holdings only, [B]).
  - It stores **broker-observed** totals and **app-attributed** quantity in separate columns.
  - Its states are `exclusive / shared / broken / unknown`.
  - Evidence comes from `broker_reads` and `broker_quantity_observations`.
  - The application never infers its ownership of fungible shares from holdings. It only checks
    the invariant `broker_total ≥ baseline_external + app_attributed`, and fails closed when that
    cannot be shown.

---

## 4. Recommended state model and transition table

### 4.1 Operation states (amended) [P]

| State | Open? | `sent_at` | Meaning |
|---|---|---|---|
| `intent` | open | NULL | Claimed. Pre-write checks running. Nothing sent. |
| `sending` | open | set | Committed immediately before the single broker call. The request may be in flight. |
| `acknowledged` | open | set | The broker returned a result (a create's id is linked). Read-back pending. |
| `unknown` | open | set | Ambiguous outcome, or a dead epoch's `sending`. Resolution by reads. |
| `needs_review` | open | set or NULL\* | Contradictory evidence. Human resolution only. |
| `confirmed` | terminal | set | Read-back proves the intended result (create: GTT active as intended; modify: target values; cancel: deleted or cancelled). |
| `adopted` | terminal | set | A create resolved by exactly one fingerprint match (inference, recorded with evidence). |
| `rejected` | terminal | set | Definite broker refusal. |
| `not_sent` | terminal | **NULL (CHECK)** | Provably not sent. Replaces `abandoned (not sent)`. |
| `absent_after_settle` | terminal, watched | **set (CHECK)**, `complete_reads_after ≥ 2` | Inferred absent. Replaces `abandoned (absent)`. |
| `resolved_manual` | terminal | any | Closed by an audited human resolution (`resolution_id` NOT NULL, CHECK). |

\* `needs_review` normally follows `unknown` or `acknowledged`, so `sent_at` is set. The existing
rule imposing no `sent_at` constraint on it is kept.

**Overlay columns (not states):** `superseded_at`, `superseded_by_kind`, `superseded_by_ref`.

**Removed:** `abandoned`, `superseded`.

### 4.2 Retry classes [P] (P§3.1 principle 3, kept)

| Kind / direction | Can it increase sell exposure? | Automatic retry after an ambiguous or definite failure |
|---|---|---|
| create | yes | **never** (`attempts ≤ 1`, existing CHECK) |
| modify, quantity up | yes | **never** (re-run pre-write checks; the user or a new op decides) |
| modify, quantity down or trailing tighten | no | yes, bounded (1, 2, 4 … ≤ 5 min, ≤ 10 attempts), each after a read-back shows the change not applied |
| cancel | no | yes, bounded as above. Retries stay on the same op (`attempts` incremented, `unknown → sending` allowed **for cancel and modify-down only**) |

### 4.3 Transition table [P]

Columns: **In flight?** means a broker write of this op may still be executing or landing.
**Auto-retry?** means whether the engine may send again without a human.

| # | Current state | Trigger | Next state | In flight? | Auto-retry? | Manual review? |
|---|---|---|---|---|---|---|
| 1 | (none) | Claim in `BEGIN IMMEDIATE`: epoch current, partition free, no exit in flight, no superseded open op, attribution allows | `intent` | No | n/a | No |
| 2 | `intent` | Pre-write check fails (unlinked SELL GTT, attribution, stale snapshot) | `not_sent` | No | create: new op allowed automatically next cycle | No |
| 3 | `intent` | Crash or restart (`sent_at IS NULL`) | `not_sent` (Stage 1, local inventory) | No | as row 2 | No |
| 4 | `intent` | Superseded (exit claim, Emergency Exit, close) | `not_sent` + overlay | No | **No**: the position no longer wants it | No |
| 5 | `intent` | CAS `intent→sending`: `superseded_at IS NULL`, no exit in flight, epoch current, committed before the call | `sending` | **Yes**, from the commit | n/a | No |
| 6 | `sending` | Response with result (create id / modify ack / delete ack) | `acknowledged` | No (response received) | n/a | No |
| 7 | `sending` | Definite rejection (`OrderException`, `InputException`, `PermissionException`; for delete, also `TokenException` per C4) | `rejected` | No | create / modify-up: no. Modify-down / cancel: yes (bounded) | create: the user may re-create |
| 8 | `sending` | Ambiguous exception (timeout, network, `DataException`, `GeneralException`, `TokenException` on create or modify, malformed body) | `unknown` | **Possibly** | create / modify-up: **no**. Modify-down / cancel: after a read-back shows it not applied | No |
| 9 | `sending` (own epoch) | Recovery cycle while `now − sent_at < timeout + 5 s` | unchanged (skip this cycle) | **Yes** | n/a | No |
| 10 | `sending` (dead or other epoch) | Stage 1, or any recovery by the current owner | `unknown` | **Possibly** (a dead process cannot send more, but a sent request may still be processed) | as row 8 | No |
| 11 | `acknowledged` | Read-back: GTT present with the intended shape (legs may differ: that is a *position* protection MISMATCH, handled by a new modify op) | `confirmed` | No | n/a | No |
| 12 | `acknowledged` | Read-back: wrong instrument, BUY leg or non-CNC; id linked elsewhere | `needs_review` | No | **No** | **Yes** |
| 13 | `acknowledged` | Read-back unreadable | unchanged; retry the read next cycle | No | read only | No |
| 14 | `unknown` (create) | Exactly one fingerprint match, unlinked, no other unlinked active SELL GTT on the instrument, read complete | `adopted` (link CAS in the same tx) | No | n/a | No |
| 15 | `unknown` (modify / cancel) | Read-back shows the target values / deleted or cancelled | `confirmed` | No | n/a | No |
| 16 | `unknown` (modify-down / cancel) | Read-back shows the change not applied | `sending` (`attempts+1`) | Yes, after the new commit | **yes** (bounded) | after the cap: **yes** |
| 17 | `unknown` (create) | Settle rule met (Q2 case 5) | `absent_after_settle` | **Not provable** (inference) | **No** | the user decides on re-create |
| 18 | `unknown` | More than one match; an unlinked active SELL GTT on the instrument; a linked-elsewhere id | `needs_review` | Possibly | **No** | **Yes** |
| 19 | `unknown` / `acknowledged` / `sending` | Superseded | unchanged + overlay | unchanged | per kind | No |
| 20 | any open + overlay | Resolution yields or reveals a GTT id | `confirmed` / `adopted` + **cancel op claimed in the same tx**; position `CLOSED_CLEANUP_REQUIRED` | No (for this op) | cancel: yes | only if the cancels exhaust |
| 21 | any open + overlay | Resolution yields no GTT (`rejected`, `not_sent`, `absent_after_settle`) | that terminal state | No | none | No |
| 22 | `needs_review` | Human resolution with a fresh complete read supporting it | `resolved_manual` (+ resolution row) | No | n/a | **Yes (this is the review)** |
| 23 | `absent_after_settle` | A late matching GTT observed | unchanged (terminal); finding `LATE_GTT_APPEARED`; protection `NEEDS_REVIEW` | Landed | **No** | **Yes** |
| 24 | any open | Process fenced (`INSTANCE_LOCK_LOST`) | unchanged; the new owner resolves it as rows 10–18 | per state | per state | No |

**Not allowed [P]:**

- any transition out of a terminal state, except the *watch* finding in row 23, which changes no
  op state;
- `unknown → sending` for a create or modify-up;
- `intent → sending` with the overlay set;
- any write by a process whose epoch is not current.

Storage-level enforcement of these (P1-Q6) is by compare-and-set predicates on the expected
`from` state inside `BEGIN IMMEDIATE`. Triggers are optional (§11.2).

---

## 5. Recommended `app_instance` design [P]

### 5.1 DDL

```sql
-- The current owner and the fencing epoch. Exactly one row, seeded by the migration.
CREATE TABLE app_instance (
  id                 INTEGER PRIMARY KEY CHECK (id = 1),
  epoch              INTEGER NOT NULL CHECK (epoch >= 0),          -- monotonic, never reused
  instance_uuid      TEXT,                                         -- uuid4, new for every process start
  pid                INTEGER,                                      -- diagnostic only
  process_created_at TEXT,                                         -- OS process creation time (UTC); with pid = process identity
  host               TEXT,                                         -- socket.gethostname()
  machine_id         TEXT,                                         -- Windows MachineGuid / POSIX /etc/machine-id; diagnostic
  lock_path          TEXT,                                         -- resolved path of the lock file held
  db_path            TEXT,                                         -- resolved DB path at acquisition
  app_version        TEXT, app_commit TEXT,
  state              TEXT NOT NULL CHECK (state IN ('none','starting','owner','stopping','stopped_clean')),
  started_at         TEXT,
  heartbeat_at       TEXT,                                         -- UTC wall clock of the last heartbeat
  heartbeat_seq      INTEGER NOT NULL DEFAULT 0,                   -- +1 per heartbeat; detects a foreign writer
  lease_seconds      INTEGER NOT NULL DEFAULT 60 CHECK (lease_seconds BETWEEN 15 AND 600),
  clean_shutdown     INTEGER NOT NULL DEFAULT 1 CHECK (clean_shutdown IN (0,1)),
  shutdown_at        TEXT,
  shutdown_note      TEXT,                                         -- e.g. 'broker write in flight at exit'
  updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO app_instance (id, epoch, state) VALUES (1, 0, 'none');

-- One row per acquisition (audit, crash history).
CREATE TABLE app_instance_history (
  epoch               INTEGER PRIMARY KEY,
  instance_uuid       TEXT NOT NULL UNIQUE,
  pid                 INTEGER, process_created_at TEXT, host TEXT, machine_id TEXT,
  lock_path           TEXT, db_path TEXT, app_version TEXT, app_commit TEXT,
  started_at          TEXT NOT NULL,
  ended_at            TEXT,
  end_state           TEXT NOT NULL DEFAULT 'running' CHECK (end_state IN
                        ('running','stopped_clean','crashed_detected','fenced','conflict')),
  previous_epoch      INTEGER,
  previous_end        TEXT,             -- how the predecessor ended, as detected at takeover
  takeover_wait_s     REAL,             -- time spent waiting for the predecessor's lease
  note                TEXT
);
```

### 5.2 Field semantics

| Field | Definition |
|---|---|
| **Instance identifier** | `instance_uuid`, a fresh uuid4 per process start. Never reused. It is the identity every fenced write compares, together with the epoch. |
| **Process identifier** | `pid` + `process_created_at`. Obtained from `os.getpid()` and the process creation time (Windows `GetProcessTimes` via `ctypes`, no new dependency; POSIX `/proc` or `psutil`). Diagnostic only. |
| **Host identifier** | `host` + `machine_id`. Two different machines on one database file (a network or synced folder) show up as different `machine_id`s. |
| **Startup timestamp** | `started_at` (UTC), written in the takeover transaction. |
| **Last heartbeat** | `heartbeat_at`, `heartbeat_seq`. Written every `HEARTBEAT_SECONDS` (proposed 15 s) in `BEGIN IMMEDIATE` with the predicate `WHERE id=1 AND epoch=:mine AND instance_uuid=:mine`. |
| **Lease / ownership expiry** | `lease_seconds` (proposed 60). **Advisory.** It never grants ownership while the OS lock is held by someone else (§7). It is used only (a) to wait out a predecessor whose state is still live, and (b) to detect a foreign writer. |
| **Fencing epoch** | `epoch`. Incremented exactly once per acquisition: `UPDATE app_instance SET epoch = epoch + 1, … WHERE id = 1` inside `BEGIN IMMEDIATE`, after the OS lock is held. The new value is read back in the same transaction. |
| **Clean-shutdown status** | `clean_shutdown`, `shutdown_at`, `shutdown_note`, `state='stopped_clean'`. Set by the shutdown hook only after authority is dropped and the loops are stopped. Set to 0 with a note if a broker write was still inside `_request` when the bounded wait expired (§7.6). |
| **Recovery after a crash** | At takeover, `state IN ('starting','owner','stopping')` means the predecessor did not finish cleanly. Its history row becomes `end_state='crashed_detected'`, the restriction reason `PREVIOUS_INSTANCE_UNCLEAN` is set, and Stage 1 treats every `sending` op of older epochs as `unknown` (row 10). |
| **Stale-instance detection** | Decided by the OS lock. If the lock is acquired, the predecessor process is gone or has released it. The heartbeat age is used only for the wait and the conflict rules in §7.2. |
| **PID reuse** | Irrelevant to correctness: liveness never uses the PID. A UI diagnostic "is the previous PID alive?" compares `pid` **and** `process_created_at`, so a reused PID with a different creation time is reported as "a different process". |
| **Two simultaneous starts** | The OS lock is the arbiter: exactly one `LockFileEx` / `msvcrt.locking(LK_NBLCK)` succeeds [W]. The loser never writes `app_instance`. If both somehow pass (a bypass, §7.2), the two epoch increments serialise under `BEGIN IMMEDIATE`. The earlier owner's next fenced transaction (heartbeat within ≤ 15 s, or any claim) fails, and it fences itself. |

---

## 6. Recommended `instrument_attribution` design [P]

### 6.1 Principles

1. **Broker-observed and app-attributed quantities are different facts and are stored apart.**
   - *Broker-observed*: what holdings and positions report, by key (B).
   - *App-attributed*: Σ of the app's open quantity for the key, from its own broker-confirmed
     entry fills minus exit fills (`position_quantity.open_quantity`) — the app's accounting (L),
     built on broker fill facts (B).
2. **Fungible shares cannot be attributed share by share** (P§6.1 [I]). The app never concludes it
   owns shares from a holdings surplus. It only checks the invariant
   `broker_total(key) ≥ baseline_external(key) + app_attributed(key)`, and refuses
   exposure-increasing automation when the invariant cannot be shown.
3. **Identity key = (account, EXCHANGE, TRADINGSYMBOL)**, with the instrument token checked, as in
   `broker_holdings` and E3 [V]. **ISIN is a linking attribute, not the key.** Reason [V]: the
   instrument cache has no ISIN, and positions, orders and GTTs act on exchange + symbol; only
   holdings may carry an ISIN [B]. A day-0 position (bought today, not yet in holdings) therefore
   has no ISIN source. **This amends P§6.2** (ISIN as the key).
   - Cross-exchange fungibility is handled as follows: an ISIN observed on holdings rows under two
     exchanges links the keys into one attribution group. A same-symbol row under another exchange
     *without* an ISIN link is `broken` (reason `AMBIGUOUS_IDENTITY`) for exposure-increasing
     automation. This is fail-closed, and stricter than today's E3 note (`same_symbol_elsewhere`).
4. **A broker snapshot is not atomic [I].** Holdings and positions are separate requests, and a
   trade between them can make them disagree. So:
   - a single inconsistent snapshot refuses *increases* immediately;
   - a *sticky* `broken` needs confirmation on a second complete snapshot (the `_reverse_seen`
     precedent, `reconciliation.py:74-80` [V]), **except** when the evidence is a broker order
     (an untagged SELL in today's book), which is direct evidence on its own.

### 6.2 DDL

```sql
-- One row per broker snapshot attempt (startup, reconciliation, pre-write, Emergency Exit, resolution).
CREATE TABLE broker_reads (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id       INTEGER NOT NULL REFERENCES accounts(id),
  purpose          TEXT NOT NULL CHECK (purpose IN ('startup','reconciliation','prewrite','emergency_exit','resolution')),
  instance_epoch   INTEGER NOT NULL,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  orders_at TEXT,    orders_error TEXT,    orders_count INTEGER,
  gtts_at TEXT,      gtts_error TEXT,      gtts_count INTEGER,   gtts_malformed INTEGER,
  holdings_at TEXT,  holdings_error TEXT,
  positions_at TEXT, positions_error TEXT,
  session_expired  INTEGER NOT NULL DEFAULT 0 CHECK (session_expired IN (0,1)),
  complete         INTEGER NOT NULL CHECK (complete IN (0,1)),   -- every required endpoint ok and well formed
  CHECK (complete = 0 OR (orders_error IS NULL AND gtts_error IS NULL AND holdings_error IS NULL
                          AND positions_error IS NULL AND gtts_malformed = 0))
);

-- Per instrument key, what one read reported (broker-observed only).
CREATE TABLE broker_quantity_observations (
  read_id            INTEGER NOT NULL REFERENCES broker_reads(id),
  exchange           TEXT NOT NULL,
  tradingsymbol      TEXT NOT NULL,
  instrument_tokens  TEXT CHECK (instrument_tokens IS NULL OR json_valid(instrument_tokens)),
  isin               TEXT,                       -- only if a holdings row carried one [B]
  settled            INTEGER, t1 INTEGER, day_net INTEGER, total INTEGER,
  problems           TEXT CHECK (problems IS NULL OR json_valid(problems)),
  untagged_buys      TEXT CHECK (untagged_buys IS NULL OR json_valid(untagged_buys)),    -- today's, not app, not app-GTT
  untagged_sells     TEXT CHECK (untagged_sells IS NULL OR json_valid(untagged_sells)),
  PRIMARY KEY (read_id, exchange, tradingsymbol)
);

-- The evaluated attribution, one row per key (overwritten per evaluation; history = the reads).
CREATE TABLE instrument_attribution (
  account_id                 INTEGER NOT NULL REFERENCES accounts(id),
  exchange                   TEXT NOT NULL,
  tradingsymbol              TEXT NOT NULL,
  instrument_token           INTEGER,
  isin                       TEXT,
  isin_group                 TEXT,                -- keys linked by an observed ISIN share one group
  baseline_external_quantity INTEGER NOT NULL DEFAULT 0 CHECK (baseline_external_quantity >= 0),
  baseline_read_id           INTEGER REFERENCES broker_reads(id),   -- the entry-time presellable proof
  app_attributed_quantity    INTEGER,             -- NULL = unknown (an app position has unknown quantity)
  broker_observed_total      INTEGER,             -- NULL = not observable this evaluation
  state                      TEXT NOT NULL CHECK (state IN ('exclusive','shared','broken','unknown')),
  reasons                    TEXT NOT NULL CHECK (json_valid(reasons)),
  sticky                     INTEGER NOT NULL DEFAULT 0 CHECK (sticky IN (0,1)),
  pending_broken_read_id     INTEGER REFERENCES broker_reads(id),   -- first snapshot that showed broken
  evidence_read_id           INTEGER REFERENCES broker_reads(id),
  evaluated_at               TEXT NOT NULL,
  review_required            INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0,1)),
  reviewed_at TEXT, review_reason TEXT, review_read_id INTEGER REFERENCES broker_reads(id), review_epoch INTEGER,
  PRIMARY KEY (account_id, exchange, tradingsymbol),
  CHECK (sticky = 0 OR state = 'broken'),
  CHECK (state != 'exclusive' OR (app_attributed_quantity IS NOT NULL AND broker_observed_total IS NOT NULL))
);
```

### 6.3 Reason codes and states

| Reason code | Evidence | Resulting state |
|---|---|---|
| — | `broker_total = baseline + app_attributed`, no untagged orders today, fresh complete read | `exclusive` |
| `UNTAGGED_BUY_TODAY` | an untagged, non-app-GTT BUY in today's book | `shared` |
| `UNEXPLAINED_SURPLUS` | `broker_total > baseline + app_attributed` with no visible order (an earlier day) | `shared` |
| `SHORTFALL` | `broker_total < baseline + app_attributed` on two consecutive complete reads, outside tolerance | `broken`, sticky |
| `UNTAGGED_SELL_TODAY` | an untagged SELL in today's book that is not linked to an app GTT | `broken`, sticky (single read suffices) |
| `UNLINKED_GTT_FIRED` | a SELL order linked to a GTT the app does not own | `broken`, sticky |
| `AMBIGUOUS_IDENTITY` | same symbol under another exchange without an ISIN link; conflicting tokens | `broken` (sticky while present) |
| `MALFORMED_ROWS` | a duplicate row or unreadable value (`broker_holdings` problems) | `broken` (not sticky; clears on a clean read) |
| `FILL_NOT_YET_VISIBLE` | an app fill reported by the order book within `FILL_VISIBILITY_TOLERANCE_SECONDS` (proposed 60 s, [B]) but not yet in positions | `unknown` (refuse increases; not yet broken) |
| `READ_FAILED` / `READ_STALE` | no complete read, or the read is older than `PREWRITE_MAX_AGE_SECONDS` (15 s) for a pre-write, or `GTT_OBSERVATION_MAX_AGE_SECONDS` otherwise | `unknown` |
| `APP_QUANTITY_UNKNOWN` | an app position's open quantity is NULL or negative | `unknown`; the position is `RECONCILIATION_REQUIRED` |

**Automation by state** (P§6.3, kept):

| State | Create / resize-up | Resize-down / cancel | Emergency Exit sale |
|---|---|---|---|
| `exclusive` | allowed | allowed | allowed |
| `shared` | allowed **only to the app's quantity** | allowed | allowed |
| `broken` or `unknown` | refused | allowed (needs only the GTT book) | refused (`unknown`: refused unless a fresh E3 snapshot shows `exclusive`) |

**Conditions requiring manual review:** a sticky `broken`; `APP_QUANTITY_UNKNOWN`;
`LATE_GTT_APPEARED` on the key. A review **requires a fresh complete read** whose evaluation
satisfies the invariant. It records `reviewed_at`, `review_reason`, `review_read_id` and
`review_epoch`, then clears `sticky`.

### 6.4 Behaviour in the required situations

| Situation | Broker-observed | App-attributed | Result |
|---|---|---|---|
| **The user manually buys the same instrument** | total rises; today an untagged BUY | unchanged | `shared` (`UNTAGGED_BUY_TODAY`, then `UNEXPLAINED_SURPLUS` on later days). Protection is never sized from holdings. Emergency Exit sells exactly the app's quantity. Never `exclusive` again while the surplus persists. |
| **The user manually sells shares** | total falls; today an untagged SELL | unchanged | `broken`, sticky. Create and resize-up refused. Resize-down to `min(app open, broker_total − baseline)` allowed. Emergency Exit refused for the instrument (E3 today). Manual review. |
| **A GTT fires outside the application** | an untagged SELL whose order is not linked to an app GTT (`trailing_engine._app_order_ids` [V]), or a shortfall | unchanged | `broken` (`UNLINKED_GTT_FIRED` / `SHORTFALL`). If the fired GTT *is* app-linked, it is not external: it is the app's GTT exit (P§7.3). |
| **Holdings and positions temporarily disagree** (T+1 roll, fill visibility, a trade between two reads) | inconsistent in one snapshot | known | `unknown` (`FILL_NOT_YET_VISIBLE` / first-sighting `pending_broken_read_id`). Increases refused immediately. `broken` only if a second complete read confirms, or the tolerance expires. Day roll: `broker_holdings` already avoids double counting overnight CNC [V]; whether a roll can be observed mid-transition is [B]. |
| **A broker response is missing or stale** | not observable | known | `unknown` (`READ_FAILED` / `READ_STALE`). No sticky change: "could not look" never proves anything. |

---

## 7. Single-instance and fencing architecture

### 7.1 Evaluation of mechanisms

| Mechanism | Prevents two local instances? | Survives a crash? | Problems | Verdict |
|---|---|---|---|---|
| **OS exclusive byte-range lock on a dedicated lock file** (`msvcrt.locking(fd, LK_NBLCK, 1)` or `LockFileEx(LOCKFILE_EXCLUSIVE_LOCK \| LOCKFILE_FAIL_IMMEDIATELY)` via `ctypes`; POSIX `fcntl.flock(LOCK_EX\|LOCK_NB)`) | **Yes**: the kernel picks one winner atomically [W] | **Yes**: released when the holder's handle closes or the process dies. Windows documents that release on termination happens "depending on available system resources", so there may be a short delay [W] | Unreliable on network shares and synced folders [I]. Must not be the DB file itself: on POSIX, closing *any* descriptor of a file drops all of that process's `fcntl` locks on it, including SQLite's own [I] | **Primary** [P] |
| Named mutex (`CreateMutexW`) | Yes, within its namespace | Yes (`WAIT_ABANDONED` on crash) | `Local\` is per logon session, so two RDP or fast-user-switch sessions could each own one; `Global\` behaviour must be verified [W]. Not bound to the DB file: the name must be derived from a path, and path aliases (subst drive, junction, UNC vs letter) defeat the derivation. Not covered by the Phase 0 guard, because it is not a file open | Rejected as primary; optional diagnostic only [P] |
| Database ownership row alone | **No.** Two processes can both read "no owner" before either commits, unless the takeover is a single `BEGIN IMMEDIATE` CAS. Even then a crashed owner needs a lease, and a lease cannot tell a paused process (sleep, debugger) from a dead one [I] | lease-based | Lease expiry would let a *paused but alive* process resume after another took over (the classic fencing problem) | Not sufficient alone; used as the fencing epoch and the heartbeat [P] |
| SQLite transaction (`BEGIN IMMEDIATE` / `EXCLUSIVE`) | Serialises writes, does not stop a second process from *running* | n/a | Holding a write transaction open for the process lifetime would block every other connection of the *same* process | Used for every fenced transaction, not as the instance lock [P] |
| PID file | No (PID reuse; stale file after a crash) | No | — | Rejected [P] |

### 7.2 Recommended architecture [P]

**Lock file.** `<DB_PATH>.instance.lock`, for example `backend/kite_terminal.db.instance.lock`.

- It is derived from the resolved `db.DB_PATH`, so a test's `tmp_path` database gets a `tmp_path`
  lock [V: `env` redirects `DB_PATH`].
- The Phase 0 guard already refuses the real one by prefix [V: `real_data_guard.py:130`].
- The lock is taken on the **file**, not on a name, so path aliases of the same file collide on the
  same lock [W].
- `.gitignore` needs an entry for it (implementation step, §13).

**Local-drive check.** At acquisition, refuse to operate (read-only, reason
`DATABASE_NOT_ON_LOCAL_DRIVE`) if the DB's volume is not a fixed local drive
(`GetDriveTypeW == DRIVE_FIXED`). Also warn on a path under a known sync root (OneDrive). P§13
residual risk 8 [P/W].

**Acquisition sequence**, the first thing in `on_startup`, after `enforce_runtime_policy`:

1. Open the lock file (`os.open(O_RDWR|O_CREAT)`) and try the non-blocking exclusive lock. If it
   fails, the process becomes a **non-owner**:
   - it never runs `init_db` writes (migrations, invariants, key minting) and never writes
     `app_instance`;
   - it starts **no** loops and **no** ticker;
   - it serves read-only pages: a status page reads `app_instance` through a read-only connection
     (`file:…?mode=ro`) to show who owns the database;
   - every write route returns `503 ANOTHER_INSTANCE_RUNNING`, **including login and callback**.
     A login rewrites the shared session row and the encrypted access token
     (`kite_client.exchange_request_token`, `kite_client.py:178-224` [V]). Whether a new Kite login
     invalidates the owner's token is [B]; it must not be allowed either way.
2. Holding the lock: `init_db()`. Migrations now run under the lock, which also closes the window
   where two starts migrate at once. Older builds are fenced out by schema version: once v11 is
   applied, a pre-Phase-2 build refuses the database (`DATABASE_SCHEMA_UNSUPPORTED` [V:
   `_check_history`]). That is useful, because older builds take no OS lock.
3. Takeover transaction (`BEGIN IMMEDIATE`):
   - read the singleton;
   - **if** `state IN ('starting','owner','stopping')` **and** the heartbeat age is below
     `lease_seconds`: commit nothing, wait read-only, and poll `heartbeat_seq` until the age reaches
     the lease **without `heartbeat_seq` advancing**;
     - if `heartbeat_seq` advances while we hold the OS lock, a second live writer exists that the
       OS lock does not see (a network or synced folder, another machine, an older build on a
       copy). The result is **`INSTANCE_CONFLICT`**: permanent read-only, URGENT, manual;
   - otherwise: `epoch = epoch + 1`, write the identity, `state='owner'`, `clean_shutdown=0`,
     `heartbeat_seq = 0`; insert the history row; mark the predecessor's history row
     (`stopped_clean` or `crashed_detected`); commit; remember `(epoch, instance_uuid)` in memory.

   The wait costs at most one lease (60 s) after a crash, and nothing after a clean shutdown.
4. Set the in-memory **write authority** flag, persist `RESTRICTED(STARTUP_RECONCILIATION_INCOMPLETE)`
   (§8), then continue the startup sequence.

**Liveness rule.** Whether an owner is alive is decided **only** by the OS lock. The DB heartbeat
never grants ownership while the lock is held elsewhere. So scenario D10 ("an old process resumes
after lease expiry") cannot arise between two processes on the same lock [I]: while the old process
is paused, it still holds the lock and nobody can take over. It remains possible only if the lock
is bypassed, and that case is caught by the epoch (§7.3) and by `INSTANCE_CONFLICT`.

### 7.3 Fencing epochs [P]

| Aspect | Design |
|---|---|
| Generation | A single integer in `app_instance.epoch`, starting at 0 (seed), incremented by 1 per successful takeover inside `BEGIN IMMEDIATE`. Never decremented, never reused. `app_instance_history.epoch` is the primary key, which enforces uniqueness. |
| When it increments | Exactly once per process start that becomes owner. Not on reconnect, re-login, reload of config or suspend/resume. A reload child is a new process and gets a new epoch. |
| How the owner proves authority | It holds the OS lock **and** its `(epoch, instance_uuid)` equals the row's. In-memory flag `authority.ok`. |
| Validation in protected transactions | Every Phase 2+ coordination transaction is `BEGIN IMMEDIATE` and starts with `SELECT epoch, instance_uuid FROM app_instance WHERE id=1`, aborting on mismatch, or puts the predicate inside its UPDATE (`… AND (SELECT epoch FROM app_instance WHERE id=1) = :mine`). This covers: the heartbeat, restriction-state writes, startup runs, attribution writes, and (Phase 3+) every op claim, transition and resolution. `BEGIN IMMEDIATE` holds SQLite's RESERVED lock for the transaction, so the check and the write are atomic against every other connection and process on the file [I: SQLite locking]. |
| Legacy writes (existing modules) | Phase 2 does not add the predicate to every existing `trade_tickets` UPDATE (dozens of sites, some reached from frozen code). Instead, authority loss stops them at the process level: loops check `authority.ok` at the start of every cycle and stop; write routes depend on `require_write_authority()`; the broker fence (below) refuses new broker writes. **Residual:** a cycle already past its authority check finishes its local DB writes. Acceptable because fencing only happens on a lock bypass; it is disclosed. An optional extra layer, a `sqlite3` authorizer that denies DML when `authority.ok` is false, is noted, but its interaction with Python's statement cache must be verified first [I]. |
| Preventing stale broker writes | **Write-fence at the choke point.** A process-wide check runs before every non-GET request: a `FencedKiteConnect._request` installed by `create_kite_connect`, composed with the existing narrowing. It refuses (a `PermissionException` subclass, `INSTANCE_NOT_OWNER`: a definite refusal, nothing sent) unless `authority.ok`. It also counts in-flight writes (§7.6). This covers the frozen `execution.py` and `strategy_engine.py:677` **without modifying them** [V: they receive the client from the factory]. For FakeKite and the paper broker, which are not narrowed, the same check sits in a thin wrapper returned by `get_kite()` around the write method names, so tests exercise it. |
| How an expired owner is kept from resuming | An owner whose heartbeat or claim transaction finds a different epoch (rowcount 0) sets `authority.ok = False` **before** anything else. Reason `INSTANCE_LOCK_LOST`: all loops stop, the write routes return 503, the fence refuses, and the UI shows a banner. It never re-acquires within the same process; recovery means restarting the process, which goes through §7.2 again. |
| Ownership change during crash recovery | The new owner's epoch is higher. Every op row stamped with an older epoch and still `sending` is treated as `unknown` (table row 10). Settle windows start from the new owner's reads. No op is ever re-sent because its epoch is old. |

**Three problems, three answers [P]:**

| Problem | Mechanism | Limit |
|---|---|---|
| Two local instances operating concurrently | OS lock (and, for older builds, the schema version) | A network or synced folder → `INSTANCE_CONFLICT` via the heartbeat, plus the local-drive refusal |
| A stale process writing the local DB | Epoch in every fenced transaction, plus process-level authority loss | A legacy local write already in progress may complete (above) |
| A delayed broker request landing after ownership changed | **Cannot be prevented by any local token [I].** Covered by: the write-ahead `sending` row (the request is never invisible); never re-sending creates or resize-ups; the successor treating older-epoch `sending` as `unknown`; settle windows in complete reads; fingerprint adoption only under the "no other unlinked SELL GTT" precondition; `LATE_GTT_APPEARED` after settle; the REST proxy must not replay requests [B] | A GTT may appear seconds to minutes later. It is always explained by an op row and handled under Q1/Q2 |

**Broker cancellation does not end races [I].** A confirmed delete proves only that the GTT is gone
at read time. A GTT that already triggered may have a working order (P§7.3 GTT-trigger row). The
design treats `triggered` on read-back as a fired GTT (`POSSIBLE_DOUBLE_SELL` check, today's C4
logic), not as a cancellation.

### 7.4 Heartbeat and suspend/resume [P]

- The heartbeat runs every 15 s from a dedicated asyncio task (with `asyncio.to_thread` for the
  transaction) as a fenced transaction: `heartbeat_at`, `heartbeat_seq+1`.
- **Suspend or hibernate.** The process is paused with the OS lock still held, so no other
  instance can start [W]. On resume, the first heartbeat sees a wall-clock gap greater than
  `3 × HEARTBEAT_SECONDS` since its own last heartbeat. It then sets
  `RESTRICTED(RESUMED_AFTER_SUSPEND)`, marks GTT observations as stale, and re-runs Stage 1 before
  new entries are allowed.
  - The gap is measured on **wall clock** and on the monotonic clock. Whether `time.monotonic()`
    advances across Windows sleep is [W], so either clock exceeding the threshold triggers it.
  - A broker request that was in flight across the suspend is covered by its `sending` row
    (Phase 3+). In Phase 2 it is covered by the existing ambiguous-outcome handling.
- **SQLite busy on the heartbeat.** A missed heartbeat does not lose ownership: the OS lock still
  holds. It only delays conflict detection. After `lease_seconds` without a successful heartbeat,
  the owner sets `RESTRICTED(HEARTBEAT_STALE)` until a heartbeat succeeds (new entries only).

### 7.5 Windows launch behaviour [P/W]

- **Launched twice** (double-clicked shortcut, a scheduled task plus a manual start): the second
  instance becomes a non-owner, stays read-only and logs `ANOTHER_INSTANCE_RUNNING`. On the
  **same** port it then fails to bind and exits. Because the lock is taken *first* in
  `on_startup`, it no longer runs `init_db` or the loops before failing [I: uvicorn order, §2.1].
- **`--dev-reload`**: the old child is killed, which releases the lock [W: timing]. The new child
  takes it, waiting out the lease if the old child died uncleanly. Reload remains development-only
  (`server.py:227-229` [V]).
- **Antivirus or indexer briefly opening the lock file**: a byte-range lock is unaffected by other
  opens without a lock [W].
- **Windows Update restart or a forced kill**: treated as a crash (D1/D2).
- **CLIs** (`db_maintenance`, `live_preflight`, …) that write the database must take the same lock
  (non-blocking; refuse if it is held) or open the database read-only (`mode=ro`). An audit of each
  CLI is an implementation step (§13).

### 7.6 Shutdown [P]

1. Set `authority.ok = False`. From now on the fence refuses new broker writes and the routes
   return 503.
2. Stop the ticker and `supervisor.stop(timeout)` (today's behaviour).
3. Wait, bounded, for the fence's in-flight write counter to reach 0.
4. In a fenced transaction: `state='stopped_clean'`, `clean_shutdown = (in_flight == 0)`,
   `shutdown_note` (for example "1 broker write still in flight at exit"); close the history row.
5. Release the lock (closing the handle).

A shutdown with a write still in flight is recorded as unclean, so the successor waits out the
lease and treats older-epoch `sending` rows as `unknown`.

---

## 8. Startup reconciliation sequence [P]

### 8.1 Persisted restriction state

Add to `account_safety_state` (or a new `account_restriction` table, one row per account):

- `restriction_state` (`NORMAL` | `RESTRICTED`)
- `restriction_reasons` (JSON array of codes)
- `restriction_since`
- `restriction_epoch`
- `last_startup_run_id`

Add a new table:

```sql
CREATE TABLE startup_reconciliation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  instance_epoch INTEGER NOT NULL,
  trigger TEXT NOT NULL CHECK (trigger IN ('startup','login','resume','manual','retry')),
  started_at TEXT NOT NULL, finished_at TEXT,
  stage1_outcome TEXT CHECK (stage1_outcome IN ('passed','incomplete','failed','blocked')),
  broker_read_id INTEGER REFERENCES broker_reads(id),
  inventory TEXT CHECK (inventory IS NULL OR json_valid(inventory)),
  findings TEXT CHECK (findings IS NULL OR json_valid(findings)),
  reasons_after TEXT CHECK (reasons_after IS NULL OR json_valid(reasons_after))
);
```

The state is **derived from persisted facts and re-derived** at every confirmation (in
`final_gate`, as a new blocker in `live_preflight.confirm_time_blockers`, next to
`RECONCILIATION_CRITICAL` [V: `account_safety.py:541-576`]) and at every reconciliation cycle. It
is never held only in memory (P§3.5). A crash during startup restarts from step 1 and never
inherits `NORMAL`, because step 4 writes `RESTRICTED` before anything else.

### 8.2 Exact sequence

| Step | Action | Pass condition | On failure |
|---|---|---|---|
| 1 | `enforce_runtime_policy()` (today) | valid runtime | abort startup (today) |
| 2 | Take the OS instance lock (§7.2) | lock held | **non-owner mode** (read-only, no loops, 503 on writes); stop here |
| 3 | `init_db()` under the lock | migrations ok | abort (today's `DATABASE_*` errors) |
| 4 | Takeover transaction: epoch + 1 (§7.2); then persist `RESTRICTED(STARTUP_RECONCILIATION_INCOMPLETE)` plus `PREVIOUS_INSTANCE_UNCLEAN` if detected | committed | `INSTANCE_CONFLICT` → read-only; stop here |
| 5 | Start the heartbeat; start the ticker (today); start the 5 loops (today) | — | loops already gate on `authority.ok` |
| 6 | **Stage 1** as a supervised task (`supervisor.spawn("startup_reconciliation")`); see 8.3 | `stage1_outcome='passed'` | stays RESTRICTED; retry with backoff (30 s, 60 s, 120 s, cap 300 s) and on every login and resume |
| 7 | **Stage 2**: recovery writes; see 8.4 | — | **no-op in Phase 2** |
| 8 | Derive the account state (8.5) and persist it | all conditions | RESTRICTED with reasons |

Starting the loops at step 5, before Stage 1 completes, is deliberate [P]:

- the loops perform today's exposure-reducing upkeep (fill following, resize-down, ratchet, cancel
  read-backs);
- RESTRICTED must never block that upkeep (P§3.5);
- they hold write authority and are fenced.

What waits for Stage 1 is **new risk**: new live entries, and (Phase 3+) automated creates and
resize-ups from recovery. This departs from P§8 step 11 ("start loops" after reconciliation). The
reason: delaying the existing loops would delay today's protection upkeep, and the existing loops
already resolve unknown entry submissions by tag and never re-send.

### 8.3 Stage 1: read-only reconciliation

Nothing in Stage 1 calls a broker write. The fence is irrelevant here because no write is attempted
[P].

1. **Local inventory (no broker).**
   - Entries in `submitting`, `unknown`, `needs_review`, `pending` or `unconfirmed`.
   - Exits in flight.
   - If the protection journal exists (Phase 3+): every non-terminal op, plus each `intent` with
     `sent_at IS NULL` → `not_sent` (table row 3, the only local write allowed, fenced) and each
     older-epoch `sending` → `unknown` (row 10).
   - Positions with unknown or negative open quantity.
   - Unresolved critical findings.
   - `instrument_attribution` rows that are sticky `broken` or have `review_required`.
2. **Connectivity.** A fresh Kite session (`kite_client.get_kite`), else `NO_SESSION`. The REST
   route is valid (`account_safety.check_rest_route`), else `PROXY_UNAVAILABLE`.
3. **One broker read** (`broker_reads` row, purpose `startup`), in this fixed order, each recording
   its timestamp, count and error: `orders()` → `get_gtts()` → `holdings()` → `positions()`.
   - `complete = 1` only if all four succeeded and are well-formed (the `broker_holdings` and
     `BrokerSnapshot` rules: a list where a list is expected, no malformed GTT rows) **and** the
     span from the first to the last response is ≤ `STARTUP_READ_SPAN_SECONDS` (proposed 30 s).
   - Record the GTT observations (today's `gtt_observations.record_book`).
4. **Attribution** per key: write the observations and evaluate `instrument_attribution` (§6), with
   the second-snapshot rule for sticky states.
5. **Classification** (today's reconciliation checks, run once against the same read):
   - order status; GTT state; broker GTTs (orphan / unlinked / duplicate / on a closed position);
   - position shortfall.
   - At startup a single complete read is enough to *report* a finding. The two-cycle debounce
     guards against the app's own non-atomic writes, and at step 6 no app write has started from
     the new process [I]. Findings are still written through the existing `log_mismatch`
     deduplication.
6. **Inconsistency detection across endpoints:**
   - an order book `filled_quantity` for an app entry that holdings and positions do not reflect
     (outside tolerance);
   - a GTT `triggered` with a linked order absent from today's book;
   - an app position whose instrument is absent from holdings and positions;
   - a token conflict.

   Each sets a reason (below), never a silent default.
7. Persist the run (`startup_reconciliation_runs`) with the inventory, findings and resulting
   reasons.

### 8.4 Stage 2: recovery writes (framework only in Phase 2)

Allowed only when **all** hold [P]:

- Stage 1 `passed` in this epoch;
- the write concerns one explicit op or position;
- the outcome is unambiguous;
- the write class allows automation (§4.2).

Permitted classes (Phase 3+):

- cancel retries;
- modify-down;
- adoption by exactly one fingerprint match;
- a create for a `PROTECTION_PENDING` position **only** after its own pre-write check.

**Never:**

- an automatic create for an `unknown` or `absent_after_settle` op;
- a retry of a create that may have been accepted;
- any write while a broker read is failing.

**In Phase 2, Stage 2 contains no writes.** It exists as a named step that records "no recovery
writes authorised in this phase".

### 8.5 Restriction reasons and the exact NORMAL condition

`RESTRICTED` reasons (all persisted) [P]:

| Code | Set when | Cleared when |
|---|---|---|
| `STARTUP_RECONCILIATION_INCOMPLETE` | step 4 | a Stage 1 `passed` in the current epoch |
| `PREVIOUS_INSTANCE_UNCLEAN` | the takeover detects an unclean predecessor | Stage 1 passed (older-epoch ops classified) |
| `INSTANCE_CONFLICT` | a foreign heartbeat advanced | **manual only** (restart after fixing the cause) |
| `INSTANCE_LOCK_LOST` | an epoch mismatch in this process | never in this process; restart |
| `DATABASE_NOT_ON_LOCAL_DRIVE` | the drive check fails | restart from a local drive |
| `HEARTBEAT_STALE` | no successful heartbeat for > lease | the next successful heartbeat |
| `RESUMED_AFTER_SUSPEND` | a heartbeat gap is detected | a new Stage 1 passes |
| `NO_SESSION` | no fresh session | login, then Stage 1 passes |
| `PROXY_UNAVAILABLE` | REST route invalid or unreachable | route ok, then Stage 1 passes |
| `BROKER_READ_FAILED` | any of the four reads failed, was malformed, or exceeded the span | a complete read |
| `BROKER_DATA_INCONSISTENT` | the cross-endpoint inconsistencies in 8.3 step 6 | a later complete read without them, or human resolution |
| `UNRESOLVED_OPERATION` | any open protection op; any entry `submitting`, `unknown` or `needs_review`; any exit outcome unknown | all resolved |
| `RECONCILIATION_REQUIRED` | any position needing a human (unconfirmed fill, unknown quantity, sticky `broken`) | human resolution |
| `CLEANUP_REQUIRED` | any `CLOSED_CLEANUP_REQUIRED` (Phase 6) or unresolved `GTT_CANCEL_*` finding | cancel confirmed / finding reviewed |
| `PROTECTION_GAP` | a protection state of PENDING, FAILED, UNKNOWN or MISMATCH older than a threshold (Phase 3+) | protected |
| `LATE_GTT_APPEARED` | §3 Q2 case 6(b) | human resolution |

**`NORMAL` holds iff [P]:**

- the process holds the lock and its epoch is current;
- a Stage 1 run `passed` in this epoch, after the last login and the last resume;
- the last complete broker read is no older than `GTT_OBSERVATION_MAX_AGE_SECONDS`;
- **none** of the reasons above is set;
- every existing confirm-time blocker passes (`check_live_critical_state`, unchanged).

`NORMAL` is necessary for a new live entry, **in addition to** `LIVE_ENTRIES_ENABLED`, which stays
`False`. So in Phase 2 the restriction can only add refusals.

### 8.6 Required situations

| Situation | Behaviour |
|---|---|
| Kite unavailable | Reads fail → `BROKER_READ_FAILED`, retry with backoff. Read-only UI shows last-known observations as STALE. Loops keep failing closed as today. No writes are attempted because reads failed; today's loops do not write without their reads [V: snapshot-None rules]. |
| Session expired | `TokenException` → `NO_SESSION` (the `BrokerSnapshot` and E3 handling [V]). Stage 1 re-runs on the next login (`trigger='login'`). Emergency Exit refuses with "log in or act in Kite" (E3 today). |
| One endpoint fails | `complete = 0`, `BROKER_READ_FAILED` naming that endpoint. The other endpoints' results are recorded and still produce findings where a check needs only them, as reconciliation does today. Nothing is concluded from the missing endpoint. Never "absent". |
| Broker responses disagree | `BROKER_DATA_INCONSISTENT`, plus attribution `unknown`; sticky only after a second complete read. |
| A GTT appears after the initial reconciliation | The next reconciliation cycle classifies it: matched to an op (Phase 3+) → adopt or resolve; matched to an `absent_after_settle` op → `LATE_GTT_APPEARED`; otherwise `UNLINKED_POSSIBLE_GTT` / `ORPHAN_GTT` (today). |
| An unresolved operation exists | `UNRESOLVED_OPERATION`. Resolution runs in the loops (by tag for entries, today; by reads for protection ops, Phase 3+). Entries stay blocked until it clears. |
| Another instance owns the lock | Non-owner mode (§7.2 step 1). No reconciliation, no writes, no loops; a read-only status page. |

---

## 9. Failure and recovery matrix [P]

"Entries blocked" means new live entries (already blocked by `LIVE_ENTRIES_ENABLED = False`; this
column states what Phase 2 adds). "Auto recovery" means without a human.

| # | Scenario | Expected local state | Broker state known? | Auto recovery? | Entries blocked? | Manual? |
|---|---|---|---|---|---|---|
| D1 | **Windows crash during startup** (before the lock, or during `init_db`) | Lock released by the OS [W]; a migration is all-or-nothing in its transaction, with a backup taken first [V: `migrate`] | Unchanged by startup (no writes yet) | Yes: the next start runs the full sequence | Yes until Stage 1 passes | No |
| D2 | **Crash after lock acquisition** (after takeover) | `app_instance.state='owner'`, `clean_shutdown=0`. Next start: waits out the lease (≤ 60 s), epoch + 1, `PREVIOUS_INSTANCE_UNCLEAN` | Unknown until Stage 1 reads | Yes | Yes until Stage 1 passes | No |
| D3 | **Crash during a DB transaction** | SQLite WAL rolls back the uncommitted transaction [I: SQLite durability]. A compare-and-set never half-applies. Ops are left in their last committed state | As before the transaction | Yes | Yes (`PREVIOUS_INSTANCE_UNCLEAN`) until Stage 1 | No |
| D4 | **Crash immediately before a broker request** (after the `sending` commit, before the bytes leave) | Op `sending` (dead epoch) → `unknown` at Stage 1. Today's entries: `submitting` → resolved by tag after 120 s [V] | **Not known**: indistinguishable from D5 locally | Reads only. Create: adopt-or-settle, never re-send | Yes (`UNRESOLVED_OPERATION`) | Only on escalation (Q2 cases 5–7) |
| D5 | **Crash immediately after a broker request** (accepted, response not recorded) | Same as D4 | Known only after reads: GTT present → adopt; order present → adopt by tag | Yes, by reads | Yes until resolved | Only on escalation |
| D6 | **Network timeout during GTT creation** | `unknown` (today: `protection_failed`, P§1 item 2 [V]) | Unknown | Adopt-or-settle; **never re-send** | Yes | `absent_after_settle` → user re-create; `needs_review` → resolution |
| D7 | **Network timeout during GTT cancellation** | Today: C4 `CANCEL_OUTCOME_UNKNOWN` finding, never retried [V]. Proposed: cancel op `unknown` → read-back → bounded automatic retry | Unknown until read-back | **Yes** (exposure-reducing) | Yes (`CLEANUP_REQUIRED`) | After the retry cap |
| D8 | **Delayed broker response** (lands after timeout, after supersede, or after ownership change) | Op `unknown` (or overlay set). The late GTT is explained by the op's fingerprint | Known once it appears in a complete read | Adopt (before settle) then cancel if superseded; after settle → `LATE_GTT_APPEARED` | Yes while open; yes with `LATE_GTT_APPEARED` | After settle: **yes** |
| D9 | **Two instances started simultaneously** | One owner (OS lock [W]); the other is a non-owner (read-only, 503 on writes, no loops, no `init_db` writes) | Unaffected | Yes | Owner: per its own Stage 1. Non-owner: refuses everything | No |
| D10 | **Old process resumes after lease expiry** | Cannot occur with the OS lock held: a paused process keeps the lock [I]. With a bypassed lock: the resumed process's first heartbeat or claim finds a different epoch → `INSTANCE_LOCK_LOST`, authority dropped, fence refuses. Its single possibly in-flight request is covered by its `sending` row | Unknown for that one request | Yes for the new owner (row 10) | Yes for the old process (permanently) | Restart the old process; investigate the bypass (`INSTANCE_CONFLICT`) |
| D11 | **Manual broker activity while offline** | Stage 1 attribution: buy → `shared`; sell or shortfall → sticky `broken`; an earlier day's activity is visible only as a quantity difference (the order book covers today only [V: E3 comment]) | Known from holdings and positions; *which* shares moved is unknowable [I] | Shared: yes. Broken: no | Yes for `broken` / `RECONCILIATION_REQUIRED` | Yes for sells |
| D12 | **Session expiry during reconciliation** | Stage 1 `incomplete`, `NO_SESSION`. Partial results recorded, never used as "absent". A reconciliation cycle aborts its checks on `TokenException` (today [V]) | Partially | After re-login | Yes | Re-login |
| D13 | **SQLite busy or locked** | Fenced transactions use `BEGIN IMMEDIATE` and wait up to `busy_timeout` (10 s). On timeout, the operation is not performed (a claim fails, nothing sent) and is retried next cycle. Heartbeat failure → `HEARTBEAT_STALE` after the lease. **A broker call is never made after a failed claim commit** (the `sending` commit precedes the call) | Unchanged | Yes | Only if prolonged (`HEARTBEAT_STALE`) | If persistent: investigate the long-running writer |
| D14 | **Broker endpoints return inconsistent data** | `BROKER_DATA_INCONSISTENT`; attribution `unknown`; sticky `broken` only after a second complete read or order evidence | Inconsistent: treated as unknown | Increases refused; resize-down and cancel allowed | Yes | If it persists beyond two cycles: yes |

---

## 10. Testing strategy (to be implemented with Phase 2, after authorisation) [P]

### 10.1 Harness rules

- **Every child process** (lock-race workers, crash victims, the shared FakeKite server) starts
  with `import real_data_guard; real_data_guard.install(); real_data_guard.neutralise_dotenv();
  real_data_guard.scrub_kite_environment()` **before any `backend` import**. This is the
  `demo_server.py` precedent [V].
- A parent-side assertion checks each child's stdout for the guard summary. The child also writes
  a marker file listing every path it opened (`real_data_guard.recording()`), and the test asserts
  no protected path appears.
- All databases, lock files and FakeKite state live under `tmp_path`. No real credentials: the
  `env` fixture's sentinels only. No broker operation: FakeKite only, and the network guard
  refuses non-loopback.
- **The `env` fixture must grant ownership.** Tests never run `on_startup`
  (`conftest.py:427` [V]). Phase 2 therefore adds a fixture step that acquires the tmp lock and
  runs the takeover transaction against the tmp database. Without it, every write-route test would
  get `503 ANOTHER_INSTANCE_RUNNING`.
  - A dedicated test asserts that a test **without** that grant is refused. Fail-closed default.
- Never call `monkeypatch.undo()` inside `env` tests (Phase 1 §6.4 lesson [V]).
- Deferred Phase 0 harnesses now become needed:
  - the `os._exit` crash harness;
  - the shared-state FakeKite (a loopback HTTP server, or a SQLite-backed state file under a
    lock);
  - day rollover, for attribution tests.

### 10.2 Test list

**CI** = runs in the normal suite on any OS. **CI-Win** = runs in the suite but only meaningful on
Windows (skipped elsewhere). **WIN-INT** = controlled Windows integration, run manually on a
dedicated machine with a scratch checkout.

| ID | Test | Type |
|---|---|---|
| L1 | Two-process startup race: N=2..8 children released by a file barrier all try the lock → exactly one owner, the rest non-owners; repeated 200× | CI (subprocess) |
| L2 | Lock auto-release after `os._exit(137)` of the owner → a new process acquires within the bound | CI; release timing WIN-INT |
| L3 | Lock file path aliases: acquire through `subst`/junction or a different-case path → collides | WIN-INT |
| L4 | Non-owner mode: no loops, no `init_db` writes, every write route 503 (login and callback included), read-only status page works | CI |
| L5 | Lock file under the guard: a test computing the real lock path is refused | CI |
| L6 | Non-local drive refusal (`GetDriveTypeW` stub, and a real network share) | CI-Win (stub); WIN-INT (share) |
| E1 | Epoch increments by exactly 1 per takeover; history unique; never reused across 100 starts | CI |
| E2 | Stale-owner write rejection: A owns epoch 5; B forced to take over (lock disabled in test) → A's next heartbeat and claim fail → `INSTANCE_LOCK_LOST`; fence refuses A's broker writes (FakeKite call count unchanged) | CI (subprocess) |
| E3 | Lease wait: an unclean predecessor with a fresh heartbeat → the new owner waits ≥ lease; an advancing foreign heartbeat → `INSTANCE_CONFLICT` | CI |
| E4 | PID-reuse simulation: predecessor row with this process's PID but a different `process_created_at` → reported as a different process; liveness still decided by the lock only | CI |
| E5 | Fence at the choke point: a real `KiteConnect` subclass with a stubbed transport → non-GET refused when `authority.ok` is false (definite `PermissionException` subclass); GET allowed; frozen-file call paths covered | CI |
| E6 | Fence wrapper for FakeKite and paper: every write method refused without authority | CI |
| S1 | SQLite contention: K threads × M processes doing fenced `BEGIN IMMEDIATE` claims on one tmp database → no lost update, all fail-closed on busy timeout, no broker call after a failed commit | CI (subprocess) |
| S2 | Deferred-transaction upgrade busy (`SQLITE_BUSY` without wait) reproduced, and shown absent with `BEGIN IMMEDIATE` | CI |
| C1 | Crash injection at named boundaries (`os._exit` in a child): before the lock; after the lock; mid-`init_db` migration; after takeover; mid fenced transaction; before a broker write; after a broker write. Then restart and assert the state per §9 | CI (subprocess) |
| C2 | Clean shutdown records `clean_shutdown=1`; shutdown with a write held in FakeKite `on_call` past the bound records 0 plus a note | CI |
| R1 | Stage 1 happy path: four reads recorded with timestamps; `passed`; `NORMAL` derivable (gate still False) | CI |
| R2 | Each endpoint failing in turn → `BROKER_READ_FAILED` naming it; nothing "absent"; retry backoff | CI |
| R3 | Session expiry mid-Stage-1 (`expire_session`) → `NO_SESSION`; re-run on login | CI |
| R4 | Malformed GTT rows or holdings → incomplete; inconsistency reasons | CI |
| R5 | Read span over the limit (FakeKite `on_call` delay) → incomplete | CI |
| R6 | Restriction persistence: crash after step 4 → restart starts RESTRICTED; never inherits NORMAL | CI |
| R7 | RESTRICTED never blocks: exits, Emergency Exit, cancels and loop upkeep continue (existing tests re-run under RESTRICTED) | CI |
| R8 | Suspend simulation: heartbeat clock jump → `RESUMED_AFTER_SUSPEND`, Stage 1 re-run | CI; real sleep/hibernate WIN-INT |
| A1–A8 | Attribution: manual buy (`shared`), manual sell (sticky `broken`), unlinked GTT fired, holdings/positions disagreement within and beyond tolerance, missing read (`unknown`), same symbol on another exchange with and without ISIN, token conflict, review clears sticky only with a fresh read | CI (A-tests need day rollover in FakeKite) |
| F1 | FakeKite delayed response: `on_call` holds `place_gtt` past the client timeout, then accepts → op `unknown` → adopted (Phase 3 engine; in Phase 2 the existing entry path's handling is asserted unchanged) | CI |
| F2 | Ambiguous outcomes (`accept_then_raise`, `SimulatedCrash`) across the fence and the epoch change: the successor never re-sends | CI (subprocess, shared FakeKite) |
| P1 | Concurrent position operations: two processes (lock bypassed in test) racing a legacy `sync_protection` and an exit claim → at most one broker write per position under the fence and epoch; documents the legacy residual | CI (subprocess) |
| Z5 | `LIVE_ENTRIES_ENABLED is False`; PK-25 hashes unchanged | CI |

**Mutation checks** (scratch copies, restored by hash, the Phase 1 practice). Each must be caught:

- lock not taken before `init_db`;
- no epoch check in the heartbeat;
- fence bypass for non-GET;
- non-owner allowed to log in;
- lease used to take over while the lock is held;
- RESTRICTED not persisted before the loops start;
- an incomplete read treated as complete;
- `sending` from an old epoch re-sent.

---

## 11. Required schema amendments (none applied; each needs separate authorisation)

### 11.1 Phase 2 migration (v11): a registered, forward-only migration [P]

**Phase 2 cannot stay dark.** The instance epoch must be in the account database to fence
transactions on it [I]. Contents:

- `app_instance` (seeded with the row `epoch = 0`) and `app_instance_history`;
- `startup_reconciliation_runs`;
- the restriction columns on `account_safety_state` (or an `account_restriction` table);
- optionally `broker_reads`, for Stage 1 evidence. `broker_quantity_observations` and
  `instrument_attribution` may also come here, or with Phase 7: a decision.

**Implications [V/I]:**

- It conflicts with Phase 1 decision D1 ("dark, not registered"): registering it **migrates the
  real account database on the next app start**. `migrate()` takes a verified backup first
  (`pre_schema_v11_bak`) [V].
- A v11 database is refused by older builds (`DATABASE_SCHEMA_UNSUPPORTED`) [V]. That fences out
  lock-less older builds, but blocks rolling back by checkout. Rollback means restoring the
  backup, or a written down-migration that also deletes the `schema_version` row.
- `test_platform_pk22.py` and `test_live_preflight.py` pin version 10 (Phase 1 §7 [V]). They must
  change.
- The Phase 1 dark functions are named `_m011_*` / `_r011_*`. If Phase 2 takes v11, they must be
  renumbered (v12+) before their own activation.

**Recommendation [P]:** v11 contains **only** Phase 2 objects, *not* the protection journal. The
journal stays dark until Phase 3 is authorised, with the §11.2 amendments applied first.

### 11.2 Amendments to the Phase 1 dark schema (for Q1–Q3) [P]

Because the table is **unregistered and empty everywhere** [V: Phase 1 §3, §6.3], the amendment is
simplest done by editing the dark DDL before registration. Its rollback refuses only while data
exists. After registration, the same change would need a table rebuild.

| Change | Reason |
|---|---|
| State enum: remove `abandoned`, `superseded`; add `not_sent`, `absent_after_settle`, `resolved_manual` | Q1, Q2, Q3 |
| CHECK `state != 'not_sent' OR sent_at IS NULL` | Q2 |
| CHECK `state != 'absent_after_settle' OR (sent_at IS NOT NULL AND complete_reads_after >= 2 AND kind = 'create')` | Q2 |
| Columns `superseded_at`, `superseded_by_kind` (CHECK in `emergency_exit`, `exit`, `close`, `user`), `superseded_by_ref`; CHECK that all three are NULL or all are set | Q1 |
| Column `resolution_id` (FK); CHECK `state != 'resolved_manual' OR resolution_id IS NOT NULL` | Q3 |
| New table `protection_operation_resolutions(id, op_id FK, outcome CHECK IN ('linked_gtt','no_app_gtt','deleted_in_kite_verified','other'), linked_gtt_id, reason TEXT NOT NULL CHECK(length(trim(reason))>0), evidence_read_id, resolver, instance_epoch, created_at)` | Q3 |
| Replace `idx_protection_ops_one_open` with the two kind-partitioned indexes (§3 Q3) | Q3 |
| Retry rule: allow `attempts > 1` only for `kind='cancel'` or a modify flagged exposure-reducing (new column `direction CHECK IN ('up','down','neutral')` for modify) | §4.2 |
| Add `updated_at` (P1-Q9) and a `reason` CHECK once its list is final (P1-Q4) | audit |
| Two `ticket_events` types `protection_superseded`, `protection_resolved` (append-suffix pattern [V]) | traceability |

### 11.3 Later phases (DDL fixed here, applied with their consumers) [P]

- `instrument_attribution`, `broker_quantity_observations` (§6.2): Phase 7, or Phase 2 if
  Stage 1 evidence is wanted earlier. Stage 1's attribution evaluation can run in memory and only
  record findings until then.
- `broker_gtt_observations` (P§5.6): Phase 5. It can reuse `broker_reads.id` as `read_id`.

---

## 12. Unresolved questions and blockers

### 12.1 Assumptions this design does **not** make

| Assumption | How the design avoids it |
|---|---|
| Kite read-after-write consistency for `get_gtts()` | The settle rule counts complete reads after recovery; `LATE_GTT_APPEARED` watch [P] |
| GTT ownership is proven by content matching | Adoption is labelled inference; allowed only with exactly one match and no other unlinked SELL GTT; otherwise `needs_review` [P] |
| Cancellation confirmation ends all execution races | A `triggered` read-back is handled as a fire; `POSSIBLE_DOUBLE_SELL` watch [P] |
| A local DB lock stops an already-sent broker request | Explicitly not: write-ahead `sending`, never re-send, successor reconciliation [P] |
| A broker holdings snapshot is atomic | Second-snapshot confirmation; read-span limit; inconsistency reason [P] |
| App-owned quantity can be inferred from fungible holdings | Never inferred; invariant check only [P] |

### 12.2 Open items

| # | Item | Label | Blocks Phase 2 implementation? | Blocks live trading? |
|---|---|---|---|---|
| U1 | **DDPI / TPIN** behaviour for GTT-triggered CNC delivery SELLs (unattended execution without DDPI; eDIS pre-authorisation with TPIN only) | [B] (P§10 item 6) | No | **Yes** |
| U2 | Byte-range lock release timing after a hard crash, and behaviour across sleep/hibernate | [W] | No: the design waits and fails closed; verified by L2 and R8 | — |
| U3 | `time.monotonic()` across Windows sleep | [W] | No (both clocks are checked) | — |
| U4 | `Global\` mutex semantics (only if the optional diagnostic mutex is built) | [W] | No | — |
| U5 | Whether a Kite login from a second client invalidates the first client's access token | [B] | No (non-owner login is refused regardless) | No |
| U6 | Whether the NET-1 REST proxy can retry or replay a non-idempotent POST/PUT/DELETE | [B]: proxy configuration review | No for Phase 2 (no new writes); **yes before Phase 3** | Yes |
| U7 | Presence and format of `isin` in Kite holdings; its absence from positions and orders | [B] | No (ISIN is optional in the design) | No |
| U8 | `get_gtts()` completeness (pagination, retention of non-active GTTs), read-after-write lag, `created_at` fields | [B] (P§10 items 9–11) | No | Yes (Phase 3 settle rule parameters) |
| U9 | uvicorn lifespan-before-bind order and reload-child behaviour | [I] | No (the lock is taken first either way) | — |
| U10 | Python `sqlite3` statement cache versus an authorizer callback (optional DML fence) | [I] | No (optional layer) | — |
| U11 | Governance: register migration v11 (touches the real DB on the next start), and renumber the dark `_m011` | decision | **Yes: must be authorised with Phase 2** | — |
| U12 | Governance: when to apply the §11.2 Phase 1 amendments (recommended: before Phase 3, not in Phase 2) | decision | No | — |
| U13 | `pywin32` is not a declared dependency; the design uses `msvcrt` and `ctypes` only (stdlib) | [V] | No | — |
| U14 | Remaining P§10 broker unknowns (items 3–5, 7, 13–15, 17–21) | [B] | No | Yes (items 3, 4, 5, 9 per P§13) |

---

## 13. Phase 2 implementation boundaries [P]

**In scope (Phase 2, once authorised):**

- `backend/instance_lock.py` (new):
  - the OS lock (Windows `msvcrt.locking` / `LockFileEx` via `ctypes`, POSIX `flock`);
  - the local-drive check;
  - the takeover transaction, epoch and heartbeat;
  - suspend detection;
  - the authority flag;
  - `require_write_authority()`;
  - the shutdown record.
- `backend/kite_client.py`: compose a write-fence into the narrowing at the `_request` choke point,
  and a fence wrapper for non-SDK brokers in `get_kite()`. Login and callback refused for a
  non-owner.
- `backend/main.py`: the new startup order (§8.2) and shutdown (§7.6); loops gated on authority.
- `backend/workers.py` / loop functions: an authority check at the start of every cycle (a
  one-line guard per loop, or a supervisor-level wrapper).
- `backend/startup_reconciliation.py` (new): Stage 1 (read-only); Stage 2 as an explicit no-op;
  persisted runs; restriction derivation.
- `backend/account_safety.py` / `backend/live_preflight.py` / `backend/diagnostics.py`: the
  RESTRICTED blocker in `confirm_time_blockers`; readiness and diagnostics display.
- `backend/db.py`: migration v11 (§11.1) and a renumbering plan for the dark `_m011`.
- Routes: a `require_write_authority` dependency on every write route.
- `.gitignore`: add `backend/*.instance.lock`.
- Tests (§10) and harness extensions (crash harness, shared FakeKite, day rollover). Update the two
  version-pinning tests.
- Docs: `DEPLOYMENT.md` (enforced single instance, local drive), the runbook (non-owner,
  `INSTANCE_CONFLICT`, RESTRICTED reasons), `TESTING.md`.
- CLI audit: each writing CLI takes the lock or opens the database read-only.

**Out of scope (later phases or never in Phase 2):**

- the protection engine, operation journal activation, adoption, fingerprinting, settle logic
  (Phase 3);
- the §11.2 dark-schema amendments (before Phase 3);
- any change to `backend/execution.py` (frozen; Phase 4, separate authorisation) or any PK-25 file;
- exit/GTT coordination, supersede and Emergency Exit changes (Phase 6);
- attribution enforcement (Phase 7). Stage 1 may *evaluate and report* attribution only;
- Stage 2 recovery writes of any kind;
- changes to existing trading behaviour other than **additional refusals** of new entries;
- `LIVE_ENTRIES_ENABLED` (stays `False`);
- any broker contact (Phase 9) or live enablement (Phase 10).

**Behavioural guarantees Phase 2 must keep:**

- RESTRICTED never blocks exits, cancellations, Emergency Exit or protection upkeep;
- with one instance and a healthy session, the only visible change is the startup restriction
  window and the new diagnostics;
- every existing test passes with the `env` ownership grant.

---

## 14. GO / NO-GO and final decision

**Evaluation against the criteria:**

| Criterion | Status |
|---|---|
| All four questions resolved | **Yes**: Q1 (§3, §4.3), Q2 (§3 table, §4.1), Q3 (§3, exact open set and partitioned index), Q4 (§5, §6) |
| Architecture internally consistent | **Yes.** The OS lock decides liveness; the epoch fences DB writes; the authority flag and choke-point fence stop broker writes; delayed requests are covered by the journal discipline. Consistent with P§3–§8, with the departures listed: ISIN not the key (§6.1); loops start before Stage 1 (§8.2); `superseded` and `abandoned` replaced (§11.2); index partitioned (§3 Q3). |
| Failure and recovery behaviour specified | **Yes**: §8.6, §9 (D1–D14) |
| No unresolved architectural blocker for implementation | **Yes.** The remaining items are (a) the **governance decision U11** (registering v11 changes the real database on the next start). It must be authorised with Phase 2 in any case, and a recommendation is given. (b) Windows behaviours (U2–U4) that the design fails closed on and that the Phase 2 tests verify. (c) Broker unknowns that do not affect a phase making no new broker writes. |

**Recommendation:**

- **GO** for authorising Phase 2 implementation **within the §13 boundaries**, with these
  preconditions:
  1. explicit authorisation of migration v11 and its effect on the real account database (U11),
     with the backup and rollback procedure written into the Phase 2 report;
  2. the WIN-INT tests (L2, L3, L6, R8) run on a controlled Windows machine before Phase 2 is
     declared complete;
  3. the proxy replay question (U6) answered before Phase 3, not Phase 2.
- **NO-GO** for live entries. `LIVE_ENTRIES_ENABLED` stays `False`. DDPI/TPIN (U1) and P§10
  items 3, 4, 5 and 9 remain live-trading blockers.

## **PHASE 2 DESIGN READY**

- All four questions are resolved.
- The architecture is internally consistent.
- Failure and recovery behaviour is specified.
- No unresolved architectural blocker remains for implementation. The v11 migration is a
  governance authorisation with a stated recommendation, not an open design question.

Phase 2 has **not** been implemented. Implementation waits for explicit authorisation.

---

## 15. Final integrity checks

Run after sections 0–14 were written. All checks are read-only.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged [V] |
| 2 | Pre-existing working-tree entries | `diff status_before.txt status_after.txt` (both `git status --porcelain=v1 --ignored`) shows exactly one added line, `?? docs/PHASE_2_DESIGN_REVIEW.md`. Every other entry (38 ` M`, the 14 earlier `??`, and all ignored files) is unchanged [V] |
| 3 | Newly created files | only `docs/PHASE_2_DESIGN_REVIEW.md` [V]. Scratch files (`status_*.txt`, `pk25.py`) are in the session scratchpad, outside the repository |
| 4 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) [V] |
| 5 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`) [V] |
| 6 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) [V] |
| 7 | Real `.env` / account database | not opened, read or copied. Only listed by name through `git status`. No test, app import or SQLite connection was made [V] |
| 8 | Broker operations | none. No Kite MCP tool, no network call to any broker, no scheduler or DMB pipeline [V] |
| 9 | Staged / committed | nothing staged (`git diff --cached --name-only` is empty), no commit, no stash [V] |
