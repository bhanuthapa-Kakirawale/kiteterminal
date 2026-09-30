# LIVE-0 — Production Readiness Report

Branch `feature/signal-detail-and-strategy-revision`, on top of `b6f8561`
(PK-26). Backups taken before the first edit: git tag `backup-pre-live0`,
branch `backup/pre-live0`, and `backend/kite_terminal.db.pre_live0_bak`
(sha256 `347b2a7b…62ba8a`).

## Executive Summary

LIVE-0 adds no strategy and changes no strategy, risk-maths, execution or
backtest code. The PK-25 freeze manifest is untouched and still verified. It
prepares the existing terminal for one controlled live CNC trade:

- **One authoritative pre-flight** (`backend/live_preflight.py`,
  `GET /api/live-readiness`, sidebar **Live Readiness**). It answers READY or
  NOT READY for a live new entry, with 30+ structured checks, and is strictly
  read-only: no DB write, no mode switch, no unlock. Broker access goes only
  through a read-only proxy, as one snapshot.
- **A narrow hard gate at confirm time.** A live new entry is refused with
  `LIVE_PREREQUISITE_MISSING` while any of these holds:
  - max order notional is unset;
  - the ₹ daily loss limit is unset;
  - the static IP is unconfirmed or contradicted;
  - an earlier live order has an unknown outcome.

  These are local checks only. Dry run, exits and Emergency Exit are never
  gated.
- **Real database migrated deliberately** v4 → v5 with a new maintenance
  command. The second start makes no change (byte-identical file).
- **Real Kite account checked read-only.** Session, account identity,
  holdings, margins, orders, GTTs and LTP were all read. The PK-14/15 payload
  assumptions hold for holdings and margins; positions had no rows that day
  (NOT VERIFIED).
- **Static IP is represented truthfully.** It is `USER_VERIFICATION_REQUIRED`:
  you chose to leave it unconfirmed and not to run the outbound-IP check.
- **A shutdown hang was found and fixed** in the tick WebSocket handler. It
  explains the tracebacks in the pre-LIVE-0 log.
- **Real-account pre-flight result: NOT READY.** Only four items remain, and
  all of them are the user's to act on:

  | Blocking item | Why |
  |---|---|
  | `STATIC_IP` | Unconfirmed |
  | `MAX_ORDER_NOTIONAL_SET` | Not set |
  | `DAILY_LOSS_AMOUNT_SET` | Not set |
  | `TRADING_LOCK` | Deliberately ON |

  Every technical check passes on the real account.

No real order, GTT, modification or cancellation was sent to Kite at any point.

## Current Baseline

| | |
|---|---|
| Before LIVE-0 | 1,559 passed, 0 failed, 0 xfailed, 0 xpassed; clean tree at `b6f8561` |
| After LIVE-0 | **1,654 passed**, 0 failed — 3 consecutive full runs |
| New tests | `tests/test_live_preflight.py` (81), `tests/test_live_readiness_ui.py` (8) |

**What already decided each question (LIVE-0A audit):**

| Question | Existing mechanism (unchanged) |
|---|---|
| Safe to start the app | `server.enforce_runtime_policy` (single worker, no reload, loopback unless explicitly allowed); `db.migrate` refuses unknown/newer schemas and takes a verified backup first |
| Safe to connect Kite | `kite_client.get_kite` / `is_session_fresh` (same IST day), token decrypted from `accounts.access_token_enc` |
| Safe to approve a live entry | `routes/tickets.confirm_and_execute_ticket` → PK-17 BUY+CNC → PK-11 displayed-mode binding → `account_safety.precheck` (lock, loss, entry limit, notional, stale signal, breaker, AMO) → atomic claim (daily slot) → `risk_engine` revalidation → `account_safety.final_gate` (lock, breaker, loss %, one LTP, price sanity) |
| Safe to submit a broker order | `paper_broker.check_mode` isolation, `execution._execute_live` |
| Aggregate view | `diagnostics.readiness()` / `GET /api/ready`: schema, integrity, critical workers *alive*, session by date, lock, breaker |

**What LIVE-0 adds on top, instead of duplicating:**
- worker **staleness** (time since last successful cycle);
- notional and ₹-loss **required**;
- static IP;
- UNKNOWN / needs-review live orders;
- critical reconciliation findings;
- strategy-history freshness vs research-only staleness;
- account identity;
- the broker payload shape;
- a real read-only broker proof.

## Real DB Migration

**The premise changed.** The packet assumed the real DB had not been started
since several packets ago. The log shows the user started the real app at
16:45 IST on 2026-09-30, after the PK-26 commit. That run:
- applied migrations v1–v4 with the automatic `pre_schema_v4_bak`;
- changed 16 startup rows, including the PK-18 plaintext-token scrub;
- logged in to Kite and synced history;
- ran one autopilot catch-up;
- was stopped with Ctrl+C at 18:04.

LIVE-0B therefore became the deliberate migration **v4 → v5** (the new
`live_readiness` table).

**Procedure:**
1. Stop the app and check there is no WAL.
2. Copy to `pre_live0_bak` and record the sha256.
3. Run a read-only audit (`mode=ro`): integrity, foreign keys, schema, per-table rows plus content hash, token counts, live tickets, lock/breaker, risk settings.
4. **Rehearse on a scratch copy.**
5. Pre-start safety audit: no live ticket in flight, no live position. The only live tickets are the four FAILED static-IP rejections.
6. `python -m backend.db_maintenance migrate` on the real DB.
7. Re-audit.
8. Second `migrate`.
9. Re-audit.

`db_maintenance migrate` is new. It:
- runs exactly `db.init_db()` with no server, workers or ticker;
- loads `.env` first;
- refuses without `KITE_TERMINAL_ENCRYPTION_KEY`, because a newly generated key would make the stored Kite token unreadable (this is tested).

| Run | Output |
|---|---|
| Rehearsal (copy) | `v4 -> v5`, applied `[5]`, 0 startup row changes; second run `none`, 0 |
| **Real, first** | `v4 -> v5`, applied `[5]`, startup row changes 0; automatic backup `kite_terminal.db.pre_schema_v5_bak` written and integrity-checked |
| **Real, second** | `v5 -> v5`, applied `none`, 0 row changes; **file byte-identical** (sha256 unchanged) |
| Post-migration | `integrity_check: ok`, `foreign_key_check: 0` |

## Real DB Before / After

| | Before (`pre_live0_bak`) | After migration (1st and 2nd) | After two real app runs |
|---|---|---|---|
| sha256 | `347b2a7b…62ba8a` | `2381e762…86ed07` (both runs) | `b9f4ec4e…69e64a` |
| size | 6,213,632 | 6,217,728 | 6,217,728 |
| schema | v4 | v5 | v5 |
| integrity / FK | ok / 0 | ok / 0 | ok / 0 |

**Row counts.** These are unchanged unless marked:
- `trade_tickets` 18
- `ticket_events` 52
- `closed_trades` 0
- `ohlc_daily` 51,708
- `ohlc_history_status` 74
- `instrument_cache` 10,159
- `sector_mapping` 52
- `watchlist` 51
- `sessions` 16
- `risk_settings` 1
- `risk_settings_history` 6
- `strategy_configs` 3
- `strategy_config_versions` 7
- `backtest_runs` 15
- `backtest_trades` 41
- `walk_forward_windows` 11
- `automation_runs` 6
- `dashboard_panels` 8
- `reconciliation_log` 0
- `paper_orders` / `paper_gtts` 0
- `schema_version` **4 → 5**
- `system_events` **8 → 9**
- `live_readiness` **new, 0 rows**

**Every intentional row change:**

| Change | Cause |
|---|---|
| `schema_version` +1 row, `live_readiness` table created | Migration v5 |
| `account_safety_state.trading_locked` 0 → 1, `system_events` +1 (`trading_locked`) | Deliberate, via `POST /api/account-safety/trading-lock`, reason "LIVE-0 readiness review: keep locked until the first live trade is deliberately approved" |
| `ohlc_history_status`, `indicator_snapshot` content refreshed | The safe history refresh on startup (`data_jobs.sync_all_symbols`, 65 symbols) |

`ohlc_daily` content is unchanged: today's final candle was already stored by
the 16:47 sync. No ticket, order, reconciliation finding, journal or
automation row was created.

**Unchanged safety settings:**
- mode `dry_run`;
- breaker closed;
- risk settings as before: notional and ₹ daily loss still NULL, which the user must set.

## Token Migration

The plaintext-token scrub had already run in the user's 16:45 start. Checked
with a script that prints booleans and counts only:

| Check | Result |
|---|---|
| `sessions.access_token` non-empty | **0** |
| `accounts.access_token_enc` present | yes |
| Decrypts with the `.env` key | yes |
| Token string in any `logs/*.log*` | **0 files** |
| Token in `GET /api/diagnostics`, `/api/live-readiness?broker=true`, `/api/session/status` | absent; the key name `access_token` is absent too |
| Session usable | yes: the backend used it for the read-only broker check (`BROKER_SESSION` PASS) |

`/api/ready` answered 503 by design, because the trading lock is ON. Older
`*_bak` files from before PK-18 still hold past plaintext tokens, as PK-18
documented.

## Read-Only Kite Connectivity

This used the user's existing same-day session (the 16:46 IST login), so no
new login was needed. There were two paths, both read-only:

1. **`python -m backend.live_preflight payload-shape`**, run twice. It writes
   a sanitized structural report: keys, types and counts only.
2. **The real app with `GET /api/live-readiness?broker=true`**, twice (0.72 s
   and 0.84 s). It takes one snapshot: `profile`, `holdings`, `positions`,
   `margins`, `orders`, `get_gtts`, one `ltp`.

Both go through `ReadOnlyKite`, which raises on any attribute outside those
seven.

**Real-account results:**

| Check | Result |
|---|---|
| Session | Kite accepted it |
| Account identity | Matched (masked in the UI) |
| Reads | 14 holdings rows, 0 positions, 0 orders, 0 GTTs |
| LTP | Read OK |

**Account state at the time:**
- no working orders, no GTTs, no positions;
- reconciliation clean;
- no UNKNOWN orders.

**Sanitized fixture:** `tests/fixtures/kite_payload_shapes.json`. It holds
key names and type names only: no values, symbols, identifiers or row counts.
A test pins the PK-14/15-read fields to it. No raw payload was saved or
committed.

## Holdings Payload Semantics

The real rows carry the following:

| Field | Type |
|---|---|
| `tradingsymbol`, `exchange`, `isin`, `product` | str |
| `instrument_token`, `quantity`, `t1_quantity`, `realised_quantity`, `opening_quantity`, `used_quantity`, `authorised_quantity`, `collateral_quantity`, `short_quantity`, `price` | int |
| `collateral_type`, `authorised_date` | str |
| `average_price`, `last_price`, `close_price`, `pnl` | int or float |
| `day_change`, `day_change_percentage` | float |
| `discrepancy` | bool |
| `authorisation`, `mtf` | dict |

All 14 rows:
- have `quantity`, `t1_quantity`, `average_price`, `last_price` and
  `close_price` present and numeric;
- **pledged (`collateral_quantity > 0`): 0**;
- **MTF quantity > 0: 0**;
- **`short_quantity > 0`: 0**;
- **`discrepancy`: 0**;
- **T1 > 0: 0**.

## Positions Payload Semantics

`positions()` returned `{"net": [], "day": []}`, because there were no
positions that day. **`product`, `quantity`, `overnight_quantity`,
`average_price` and `last_price` could not be observed in real data: NOT
VERIFIED.**

The pre-flight's payload check would flag a CNC row missing numeric
`quantity`/`overnight_quantity` on any later read. This is tested with a
synthetic payload.

## Margins Payload Semantics

`equity` and `commodity` segments:

| Key | Content |
|---|---|
| `enabled` | bool |
| `net` | float (equity) |
| `available` | `adhoc_margin`, `cash` (float), `collateral`, `intraday_payin`, `live_balance` (float), `opening_balance` (float) |
| `utilised` | `debits`, `delivery`, `equity`, `exposure`, `holding_sales`, `liquid_collateral`, `m2m_realised`, `m2m_unrealised`, `option_premium`, `payout`, `span`, `spread_benefit_nfo`, `stock_collateral`, `turnover` |

## PK-14/15 Assumption Check

| PK-14/15 reads | Real payload | Verdict |
|---|---|---|
| `holdings[].quantity` + `t1_quantity` as held shares | Present, int, all rows | **Holds** |
| `last_price`, falling back to `close_price` | Both present, numeric | **Holds** |
| Pledged shares assumed inside `quantity` (collateral fields not read) | `collateral_quantity` present; 0 pledged rows | **Holds for this account today.** Not provable in general; the pre-flight now warns (`HOLDINGS_NEED_REVIEW`) if any row is pledged, MTF, short or flagged |
| `positions.net` CNC `quantity − overnight_quantity` | No rows | **NOT VERIFIED** |
| `margins.equity.net`, `margins.equity.available.collateral` | Present, numeric | **Holds** |
| Missing numeric field → silently 0 (risk_engine behaviour) | — | The pre-flight now **blocks** (`BROKER_PAYLOAD_SHAPE`) if a field the maths reads is missing or non-numeric |

**No material contradiction. No blocking finding. `risk_engine.py` was not
touched**, as it is frozen.

**Observation.** Exposure counts only live positions: `risk_engine`
explicitly excludes dry-run tickets. In the paper rehearsal a second INFY
ticket saw 0.0% current exposure while a paper INFY position was open. This
is the designed PK-20/PK-14 isolation, not a defect.

## Static IP Readiness

| | |
|---|---|
| Deployment | **Mode A, local**: the launcher's security summary reads `bind=127.0.0.1 remote_exposure=no reload=no workers=1 started_by=launcher`. This matches DEPLOYMENT.md. |
| Kite API exposes the whitelisted IP? | **No**, so it cannot be verified by the app from Kite. |
| Recorded state | **`USER_VERIFICATION_REQUIRED`**. You chose to leave it unconfirmed. |
| Outbound-IP observation | **NOT RUN**, by your decision. No third-party request was made. |
| Effect | Live Readiness blocks (`STATIC_IP` = ACTION REQUIRED), and the confirm gate refuses live entries until you confirm it on the page. |

**How the states work:**

| State | Meaning | Effect |
|---|---|---|
| `VERIFIED` | The user's attestation plus a recent (≤24 h) outbound-IP observation that matches | Passes |
| `USER_CONFIRMED` | Attested only | Passes, with the observation recommended |
| `USER_VERIFICATION_REQUIRED` | Not attested | Blocks |
| `FAILED` | A recent observation differs | Blocks |

Only public (globally routable) IPs are accepted. The observation is one
`GET https://api.ipify.org`, made only when the user clicks for it.

## Live Preflight Architecture

`backend/live_preflight.py`:

- **`evaluate(broker, refresh, now)`** returns:
  - `overall_status`, `ready_except_trading_lock`, `blocking_codes`, `warning_codes`;
  - `execution_mode`, `broker_checked`, `checked_at`;
  - `disclaimer`, `sections`;
  - `checks[{code, section, status PASS|FAIL|WARN|USER_ACTION, blocking, message, action_link, detail}]`.

  READY means no blocking check is FAIL or USER_ACTION.
- **`ReadOnlyKite`** holds a seven-method allow-list. **`BrokerSnapshot`** holds one read each, and a failed read becomes `None` plus an error, never an empty list. The snapshot is cached for 30 s (`PREFLIGHT_BROKER_SNAPSHOT_CACHE_SECONDS`).
- **Shared with the gate**, so the page and the gate cannot disagree: `static_ip_status()` and `unresolved_live_submissions()`.
- **Thresholds are central, in `config.py`:**
  - `WORKER_STALE_AFTER_SECONDS`: order monitor 120 s, trailing 180 s, reconciliation 600 s, paper 120 s;
  - `EGRESS_*`.
- **Routes**, in `backend/routes/live_readiness.py`:
  - `GET /api/live-readiness?broker=&refresh=`;
  - `POST …/static-ip {ip, confirm:true}`;
  - `POST …/static-ip/clear`;
  - `POST …/egress-check`.

  The POSTs are CSRF-guarded, require the trading account, and work without a Kite session.
- **Migration v5 `live0_live_readiness`** adds `live_readiness(account_id PK, static_ip, static_ip_confirmed_at, egress_ip, egress_checked_at, egress_error, updated_at)`. It seeds nothing.
- **UI**: `frontend/readiness.js` holds the pure `viewModel()` plus a DOM renderer; it uses `textContent` only and makes no inline scripts or styles. Also: a nav item, links to and from Diagnostics, and the Settings **FIRST LIVE / CAUTIOUS** profile (it fills the form only).
- **Research isolation**: `live_preflight.py` imports no research module and contains none of the guarded strings.

## Blocking Checks

| Section | Code | Blocks when |
|---|---|---|
| Database | `DB_SCHEMA_CURRENT` | pending, newer or unknown schema |
| Database | `DB_INTEGRITY` | `quick_check` not ok |
| Security | `RUNTIME_POLICY` | reload on, or policy violation; a bare-uvicorn start is WARN |
| Security | `ALLOWED_HOSTS` | remote bind with loopback-only hosts |
| Broker | `BROKER_SESSION` | no same-day session (FAIL), not yet proven (USER_ACTION), or Kite refuses `profile()` |
| Broker | `ACCOUNT_IDENTITY` | the account Kite reports ≠ the session's; not the trading account |
| Broker | `BROKER_READS` | a read-only call failed |
| Broker | `BROKER_PAYLOAD_SHAPE` | a field the exposure maths reads is missing or non-numeric |
| Static IP | `STATIC_IP` | unattested, or FAILED |
| Market data | `MARKET_DATA_LTP` | no usable LTP (or not checked) |
| Market data | `CALENDAR_YEAR` | current year not covered |
| Market data | `STRATEGY_HISTORY_FRESH` | a watchlist symbol lacks the latest completed session's candle |
| Workers | `WORKER_ORDER_MONITOR`, `WORKER_TRAILING`, `WORKER_RECONCILIATION` | not running, never cycled, stale past the threshold, or failing |
| Account safety | `MAX_ORDER_NOTIONAL_SET`, `DAILY_LOSS_AMOUNT_SET`, `DAILY_ENTRY_LIMIT_SET`, `PRICE_DEVIATION_GUARD_SET` | NULL |
| Account safety | `CIRCUIT_BREAKER` | open |
| Reconciliation | `RECON_LAST_CYCLE` | never, stale, or could not read the GTT book or holdings |
| Reconciliation | `RECON_CRITICAL_UNRESOLVED` | an unresolved finding in `lifecycle.CRITICAL_ISSUES`, or an unclassified needs-review row |
| Reconciliation | `UNKNOWN_LIVE_ORDERS` | a live UNKNOWN, needs_review, stale submitting or unconfirmed-fill ticket |
| Reconciliation | `PAPER_LIVE_ISOLATION` | a ticket carries the other mode's broker ids |
| Trading lock | `TRADING_LOCK` | ON (USER_ACTION); `ready_except_trading_lock` tells the UI that this is the last step |

**The confirm-time gate** (`account_safety.check_live_prerequisites`) runs in
`precheck` for live new entries only, right after the lock check. It checks
notional and ₹ loss set, static IP acceptable, and no unresolved live
submissions. It has no broker call.

## Warning Checks

These never block:

| Code | Meaning |
|---|---|
| `RUNTIME_POLICY` | Unverified bare-uvicorn start |
| `TICKER_CONNECTED` | Tick stream down |
| `WORKER_PAPER` | Paper worker not healthy |
| `HISTORY_NEEDS_REFRESH` | Watchlist symbols flagged; the strategy scan skips them |
| `RESEARCH_HISTORY_STALE` | Non-watchlist research-only symbols; on the real account today: 9 symbols |
| `RECON_NONCRITICAL_UNRESOLVED` | Non-critical reconciliation finding |
| `BROKER_OPEN_ORDERS_UNLINKED` | Working Kite orders not placed by the terminal |
| `HOLDINGS_NEED_REVIEW` | Pledged, MTF, short or discrepancy rows |

`EXECUTION_MODE` (DRY RUN) is shown as ACTION REQUIRED but is non-blocking,
and it is never switched.

## Safety Settings

| Setting | Real DB now | Required for first live |
|---|---|---|
| Max order notional | **not set** | yes: the user enters it |
| Daily loss limit ₹ | **not set** | yes: the user enters it |
| Max new live entries/day | 3 | review; the cautious profile fills 1 |
| Max open positions | 10 | review |
| Max stock exposure | 10% | review |
| Max sector exposure | 20% | review |
| Entry price deviation | 3% | set |
| Execution mode | DRY RUN | the user switches deliberately |
| Trading lock | **ON** (LIVE-0) | the user unlocks deliberately |

**The FIRST LIVE / CAUTIOUS profile** (Settings) runs only after a
confirmation dialog. It:
- sets 1 entry/day in the form;
- makes the two ₹ fields required;
- never saves and never chooses an amount (browser-verified: 0 settings requests).

**Semantic change, deliberate:** before LIVE-0, NULL notional or NULL ₹ loss
meant "no limit" for live entries. Now a live entry is refused while either
is unset. PK-12 tests updated for this:
- `test_12b_unset_cap_no_longer_means_unlimited_for_a_live_entry` (was `…disabled_cap_does_not_limit`);
- `test_12b_malformed_or_negative_setting_is_rejected`, which now compares with the prior value;
- `test_12c_unknown_live_submission_counts`, where the refusal now lists `LIVE_PREREQUISITE_MISSING` first, then `DAILY_ENTRY_LIMIT`;
- `test_12f_unset_by_default_and_required_for_a_live_entry` (was `…disabled_by_default`);
- in the integration tests, s7 now asserts both codes, and s9/s10 include `LIVE_PREREQUISITE_MISSING` in the ordered list.

Three tests that need several UNKNOWN outcomes to exercise *another* control
switch only the UNKNOWN part off, with a documented helper
`_ignore_live0_unknown_gate`:
- the breaker count;
- two daily-slot race tests.

`conftest.env` seeds deliberately generous prerequisites so unrelated live
tests are unaffected.

## Worker Health

On the real app, both starts:
- the 5 loops started exactly once;
- `order_monitor` PASS, cycled 4–13 s before;
- `trailing` PASS, 19–28 s before;
- `reconciliation` PASS, 63–73 s before;
- paper PASS;
- ticker connected.

Worker health is process memory. After the restart the timestamps recovered
within one cycle.

## Reconciliation State

On the real account:
- the last cycle read Kite's GTT book (0 active) and holdings;
- **0 unresolved findings** (critical or not);
- 0 UNKNOWN or needs-review live tickets;
- paper/live isolation intact.

LIVE-0 does not run reconciliation itself: it reads the worker's own
read-only-to-Kite result. No cancellation or modification was made.

## Market / History Freshness

- The startup sync refreshed all 65 symbols (holdings plus watchlist) through
  the existing provider path (`data_jobs`), with no warnings or errors.
- **All 51 watchlist symbols have the 2026-09-30 candle**, the latest completed session.
- The NSE calendar covers 2026, and `latest_completed_session` is 2026-09-30.
- No NEEDS_REFRESH flag on any watchlist symbol.
- 9 research-only symbols are stale: WARN, never blocking.
- PK-26's "latest candle 2026-09-22" finding is resolved for the strategy universe.

## Paper End-to-End Rehearsal

This ran the **real application code on a scratch copy of the migrated real
DB** (`rehearsal_server.py`), with these substitutions:
- FakeKite as the only broker, which cannot reach Kite;
- a throwaway encryption key, so the real one is never loaded;
- the NSE clock pinned open;
- deterministic paper price controls.

The real DB got no rehearsal rows. It was driven over HTTP by
`rehearsal_drive.py`.

| # | Step | Result |
|---|---|---|
| 1 | Readiness examined (NOT READY on the copy); mode DRY RUN | PASS |
| 2 | BUY+CNC 2 INFY created; risk checks passed | PASS |
| — | The inherited trading lock refused approval (`TRADING_LOCKED`); lock released **on the copy only** | PASS |
| 3–4 | Approved in DRY RUN → paper order `PAPER-O-1` working; positions view ENTRY_OPEN | PASS |
| 5–6 | Price evidence → fill at 1499.8 → **OPEN** | PASS |
| 7 | Paper GTT `PAPER-G-1` active, both legs = filled quantity 2 | PASS |
| 8 | Positions row: DRY RUN, paper, PROTECTED, protection qty 2 = open qty 2 | PASS |
| 9 | Exposure as designed (dry run excluded from the live account's exposure) | PASS (see note above) |
| 10–12 | Exit ticket created → approved → **EXITING**, no journal row yet | PASS |
| 13–14 | Exit fill → **CLOSED**; exactly one DRY RUN journal row; another cycle adds none | PASS |
| 15 | No reconciliation finding | PASS |
| — | FakeKite order and GTT books empty; calls were only `get_gtts`, `holdings`, `ltp`, `margins`, `positions` | PASS |

## Restart Rehearsal

**Paper (scratch copy):**
- one position OPEN (TCS) and one EXITING (INFY) → graceful stop (5 workers stopped, 0 timed out) → restart;
- both states preserved;
- `paper_orders` 5, `paper_gtts` 3, `closed_trades` 1: unchanged, **no duplicates**;
- the exit filled once → CLOSED, exactly one new journal row;
- TCS still OPEN;
- one worker set started;
- 0 broker mutations.

**Real app, real DB (LIVE-0K):**
- start → readiness → graceful stop → start → readiness → graceful stop;
- each start: `applied_migrations=none schema_version=5 startup_row_changes=0`, `started_by=launcher`, one `worker.started`;
- each stop: `worker.stopped count=5 timed_out=0`, no traceback;
- no python process or port-8000 listener left behind;
- the trading lock (since 14:47:08 UTC), settings and mode persisted;
- readiness recovered after the restart.

**Shutdown hang found and fixed** (`backend/routes/ws.py`):

| | |
|---|---|
| Defect | The `/ws/ticks` handler only awaited its outbound queue. With no ticks flowing it never saw a closed tab, nor the server's own shutdown close. |
| Consequence | A graceful shutdown waited on it while a browser tab stayed open (reproduced: about 40 s, until the probe closed the browser). This is the likely cause of the `KeyboardInterrupt`/`CancelledError` tracebacks at `ws.py:28` in the user's 18:04 Ctrl+C, where a second Ctrl+C forced the quit. |
| Fix | The handler now also watches `websocket.receive()` for the disconnect. |
| Verified (real server) | With a tab open, the handler logged `disconnected: code=1012`, the workers stopped, and the process exited about 0.5 s after the stop request, with no traceback. |
| Tests | An ASGI-level regression test (proven by mutation), plus end-to-end and ordering tests. |

## Browser Validation

Playwright with headless Edge, against a fresh demo server with enforced CSP.
**18/18 PASS** (final run):

| # | Check | Result |
|---|---|---|
| 1 | Live Readiness loads and routes (`#live-readiness`) | PASS |
| 2 | NOT READY with reasons | PASS |
| 3 | Blocking and warning lists separate; rows typed Blocking/Advisory; failing sections open | PASS |
| 4 | No sensitive value in the page (`demo_api_secret`, `fake-access-token`, user id, `access_token`, `api_secret`) | PASS |
| 5 | Trading lock OFF → ON shown correctly | PASS |
| 6 | "Open Settings" link → Settings | PASS |
| 6b | Cautious profile: fills 1/day, makes ₹ fields required, **saves nothing** | PASS |
| 7 | Diagnostics link, and Diagnostics → Live Readiness | PASS |
| 8 | Static-IP manual verification shown; confirming via the page gives USER_CONFIRMED (not auto-VERIFIED) | PASS |
| 9 | Operational disclaimer shown | PASS |
| 10 | No order, ticket, mode or kill-switch request from the page (only `POST /api/live-readiness/static-ip`, plus the test's own settings save) | PASS |
| 10b | READY shows the 10-item checklist, 0 pre-ticked; the verdict is not a green "GO" | PASS |
| 11 | No CSP violation | PASS |
| 12 | Console and page errors clean | PASS |
| 13 | 420 px: view 332/332, no overflowing element | PASS |

**Found and fixed during validation:** at 420 px the checks table overflowed
(497 px in a 332 px view), because of the global `table { min-width: 480px }`.
`.rd-table` now overrides it. The global top-bar overflow at 420 px is
pre-existing and was left alone, as instructed.

## Mutation Results

`mutations_live0.py`: each mutant is applied to one file, `-x` runs the LIVE-0
tests, and the original bytes are restored in `finally`. Every restored file
was **sha256-identical**.

| # | Mutation | File | Caught | First catching test |
|---|---|---|---|---|
| 1 | Pre-flight ignores the trading lock | live_preflight.py | ✔ | `test_11_trading_lock_blocks` |
| 2 | Unset max notional allowed (pre-flight) | live_preflight.py | ✔ | `test_12_max_notional_unset_blocks` |
| 3 | Unset max notional allowed (confirm gate) | account_safety.py | ✔ | `test_gate_refuses_a_live_entry_with_every_prerequisite_missing` |
| 4 | Unset daily loss allowed (pre-flight) | live_preflight.py | ✔ | `test_13_daily_loss_unset_blocks` |
| 5 | Unset daily loss allowed (confirm gate) | account_safety.py | ✔ | `test_gate_refuses_a_live_entry_with_every_prerequisite_missing` |
| 6 | UNKNOWN live order allowed | live_preflight.py | ✔ | `test_24_unresolved_live_unknown_blocks_readiness_and_new_live_entries` |
| 7 | Dead order monitor allowed | live_preflight.py | ✔ | `test_16_dead_order_monitor_blocks` |
| 8 | Critical reconciliation issue allowed | live_preflight.py | ✔ | `test_19_critical_unresolved_issue_blocks` |
| 9 | Static-IP unknown treated as READY | live_preflight.py | ✔ | `test_09_unknown_static_ip_is_action_required_and_blocks` |
| 10 | Pre-flight calls `place_order` | live_preflight.py | ✔ | `test_07_read_only_diagnostics_never_place_an_order` |
| 11 | Read-only proxy lets mutations through | live_preflight.py | ✔ | `test_07b_the_read_only_proxy_refuses_every_mutation[place_order]` |
| 12 | Stale strategy history allowed | live_preflight.py | ✔ | `test_22_stale_strategy_history_blocks` |
| 13 | Tick socket ignores the disconnect (shutdown hang) | routes/ws.py | ✔ | `test_the_tick_handler_ends_on_disconnect_while_no_tick_flows` |

**13/13 caught.** Mutant 13 at first **survived** against a TestClient-only
test, because the test client cancels the handler itself. The ASGI-level
regression test was added, and the mutant was then caught.

## Targeted Repeats

| Subset | Runs | Result |
|---|---|---|
| LIVE-0 suites (`test_live_preflight.py` + `test_live_readiness_ui.py`, 89 tests) | ×10 | **10/10 green** (~12 s each) |
| No-order / readiness-block subset (34 tests: read-only proxy, zero mutations, writes-nothing digest, never-unlocks, static IP, lock, notional, loss, dead worker, critical recon, stale history, UNKNOWN, gate, tick socket) | ×20 | **20/20 green** |
| Restart / migration subset (25 tests: PK-22 migration/restart/backup/maintenance + LIVE-0 v5 migration and `migrate` CLI) | ×10 | **10/10 green** |

Also run once, after the WebSocket fix: `test_platform_pk22.py`,
`test_csrf_protection.py` and `test_security_hardening.py`, 366 passed.

## Full Suite ×3

| Run | Result |
|---|---|
| 1 | **1,654 passed**, 0 failed (359 s) |
| 2 | **1,654 passed**, 0 failed (360 s) |
| 3 | **1,654 passed**, 0 failed (392 s) |

That is 1,559 at baseline plus 95 new or parametrised tests. There were no
xfail and no skips of LIVE-0 tests. The PK-25 freeze tests
(`test_smart_money_validation` 24–26) pass on every run.

## Real Account Items Still Requiring User Verification

| Item | Status | What the user must do |
|---|---|---|
| Static IP whitelisted for this Kite app | **USER_VERIFICATION_REQUIRED** | Developer console → note the IP → Live Readiness → Static IP → enter, tick, Confirm. Optionally *Check this machine's outbound IP* → must be VERIFIED, never FAILED. |
| Outbound-IP observation | NOT RUN (by choice) | Optional, as above |
| Positions payload semantics (`overnight_quantity`, CNC `product`) | **NOT VERIFIED**: no positions that day | Re-run `python -m backend.live_preflight payload-shape` on a day with a position (e.g. after the first fill); `problems` must be empty |
| Pledged, MTF or short holdings | Not present today | If they appear later, `HOLDINGS_NEED_REVIEW` warns; review before trading those symbols |
| Max order notional and daily loss ₹ | Not set | Settings (cautious profile) → enter your own amounts → Save |
| A real live order path through Kite (entry, GTT, fill, exit) | **Not exercised**: nothing was placed | FIRST_LIVE_TRADE.md |

TESTED (mocked) is distinct from REAL ACCOUNT VERIFIED:
- **Real account verified:** session, identity, holdings and margins shape, LTP, empty order and GTT books, reconciliation cycle, history refresh, worker health, restart.
- **Tested only:** every refusal path, positions semantics, UNKNOWN handling, static-IP VERIFIED/FAILED logic, the live order path.

## First-Live-Trade Preconditions

In order:
1. Confirm the static IP.
2. Set notional and ₹ loss (the cautious profile).
3. Run Live Readiness with the broker check. Only `TRADING_LOCK` may remain.
4. Switch to LIVE.
5. Create one small BUY+CNC ticket and inspect it.
6. Unlock.
7. Confirm READY and the manual checklist.
8. Approve **one** ticket.
9. Verify the fill, GTT and quantity in the terminal **and** in Kite.
10. Re-lock.
11. No second live trade until the first round trip is verified.

The full protocol and the failure protocol (REJECTED, UNKNOWN, PARTIAL, OPEN
too long, GTT missing, quantity mismatch, possible double sell) are in
[FIRST_LIVE_TRADE.md](FIRST_LIVE_TRADE.md).

## Known Limitations

1. **Static IP cannot be read from Kite.** The strongest available state is
   the user's attestation plus a matching outbound-IP observation. A VM
   behind changing NAT can differ at order time; the 24 h observation window
   limits but does not remove this.
2. **The session is judged by same-day login in the confirm gate.** Only the
   pre-flight (`broker=true`) proves it with a live call.
3. **Worker health is in-process memory.** The pre-flight answers for the
   process serving it. There is still no single-instance lock (PK-18
   limitation).
4. **The pre-flight uses the worker's latest reconciliation result.** It does
   not run a fresh reconciliation of its own, so a finding can lag by up to
   one cycle (180 s).
5. **`needs_review` live tickets have no in-app resolution action**, which is
   existing behaviour. While one exists, new live entries are blocked by
   design; resolution means comparing with Kite. The real DB has none.
6. **Positions payload is NOT VERIFIED** with real data (see above).
7. **LIVE-0L (a persisted "first live verified" guard) was not implemented.**
   The cautious profile (1 new live entry/day), the trading lock and the
   written protocol provide the same control without new lifecycle state.
8. **`APP_VERSION` is now `live0`**; it had not been bumped since PK-22.
9. **The global top-bar overflow at 420 px is pre-existing and out of scope.**

## Deferred PK-27 / PK-28

PK-27 (derivatives intelligence) and PK-28 (options context) were not started,
as instructed. None of the following was added:
- new strategy logic;
- Smart Money v1.1;
- options, short selling or MIS lifecycle;
- multi-account support;
- automated execution;
- an automatic first live trade;
- automatic unlock.
