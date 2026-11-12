# U6 mitigation report: HTTP redirects disabled for Kite requests (M1 + M2)

**Status:** see §9. **U6 mitigation: PASS.** **Phase 3: NO-GO** (other prerequisites are still open).
`LIVE_ENTRIES_ENABLED` is `False`, and live trading stays **NO-GO**.

This implements mitigations M1 and M2 from `docs/U6_PROXY_REPLAY_INVESTIGATION.md` §8 (cited as "U6§n"). That
report stays unchanged as the historical investigation. Scope: M1 and its tests only. Nothing from Phase 3 was
started: no GTT protection engine, no protection recovery, no journal-driven writes, and no change to
migration v11 (no v12).

## 0. Starting state (recorded 2026-11-08, before any edit)

| Item | Value |
|---|---|
| Repository / branch | `D:/Claude_WS/kite-terminal` / `feature/signal-detail-and-strategy-revision` |
| HEAD | `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` |
| Working tree | 96 `git status --short` entries (69 ` M`, 27 `??`); 147 lines including ignored entries. The inventory and a sha256 of every tracked and untracked non-ignored file were saved to the session scratchpad (`before.json`). |
| `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) |
| PK-25 frozen hashes | 13 / 13 match (sha256, CRLF→LF, `research/pk25/validation_manifest.json` → `model_freeze`) |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) |
| Protected files | `.env`, `backend/ui_prefs.json` and 41 `backend/kite_terminal.db*` files: size and mtime recorded with `os.stat` only, never opened |
| Pre-edit copy | `backend/kite_client.py` copied to the scratchpad before the first edit |
| Standing backup practice (git tag and branch, database copy) | **Not done, deliberately.** This task forbids git writes other than the edits themselves, and forbids database access. The pre-edit copy and the before/after hashes replace it for the one edited file. |

## 1. The code change (M1)

One production file changed: `backend/kite_client.py`, **+17 / −1** lines against the pre-edit copy. Its CRLF
line endings are preserved, with no mixed endings.

```python
# backend/kite_client.py:198-210 (new)
# ---- U6 M1: no HTTP redirect is ever followed (docs/U6_MITIGATION_REPORT.md) ----
# ... (8 comment lines: why, and what the exception means for writes and reads)
def _no_redirects(kite):
    if isinstance(kite, _SDK_KITE_CONNECT):  # a test or demo stand-in has no requests session
        kite.reqsession.max_redirects = 0
    return kite

# backend/kite_client.py:212-229, create_kite_connect (changed lines only)
    if proxy is None:
        # Direct: reads only. No broker write ever leaves this machine without the proxy.
        return fence_broker(_restrict_if_writes_disabled(_no_writes_on_direct_route(
            _no_redirects(KiteConnect(api_key=api_key)))))
    kite = KiteConnect(api_key=api_key, proxies=proxy.requests_proxies)
    kite.reqsession.trust_env = False
    kite.reqsession.max_redirects = 0  # U6 M1: see _no_redirects
    return fence_broker(_restrict_if_writes_disabled(kite))
```

- **Proxied client** (the only route that may write): the literal `kite.reqsession.max_redirects = 0`,
  next to `trust_env = False`, as U6§8 specified.
- **Direct client** (reads, login, dry run; every write except the login token exchange is refused locally):
  the same setting through `_no_redirects`. It is applied too because the direct client still sends the
  login's `POST /session/token`, and because "every client the factory builds" is simpler to verify than
  "every client that can write".
- `_no_redirects` checks `isinstance(kite, KiteConnect-SDK)`. The demo and some tests replace the module name
  `KiteConnect` with a stand-in that has no `reqsession` (`test_proxy_mandatory.py::test_17`). This is the
  same guard `_narrow` already uses.
- `create_kite_connect` is the only place a `KiteConnect` is constructed (`test_kite_rest_proxy.py::test_14d`,
  which still passes with its two constructions). Every client the application obtains therefore has the
  setting:
  - `get_kite`, and through it `require_kite` / `require_trading_account`, every route, loop and Emergency
    Exit;
  - `build_login_url`;
  - `exchange_request_token`.
- **Unchanged:**
  - the class of each client;
  - the Phase 2 write fence (`fence_broker`);
  - the narrowing (`DirectRouteKiteConnect` / `ReadOnlyKiteConnect`);
  - the proxy mapping, `trust_env`, timeout (7 s) and TLS verification.
- **Not touched:**
  - `backend/execution.py` and the other 12 PK-25 files;
  - `backend/live_entry_gate.py`;
  - migrations;
  - installed packages;
  - the frontend.

## 2. Why `max_redirects = 0` prevents redirect replay

Installed versions: kiteconnect 5.0.1, requests 2.34.2, urllib3 2.7.0.

1. `KiteConnect._request` always calls `reqsession.request(..., allow_redirects=True, ...)`
   (`kiteconnect/connect.py:904-913`). The application cannot change that per call. It can change the
   session.
2. `requests.Session.send` sends the request once through the adapter. urllib3's own redirect handling is
   off (`redirect=False`). Then, because `allow_redirects` is set, it calls `resolve_redirects`.
3. `resolve_redirects` (`requests/sessions.py:186-219`) does the following on its **first** iteration, before
   it builds or sends anything:
   ```python
   resp.history = hist[:]          # [] on the first redirect
   hist.append(resp)
   resp.content                    # reads the 3xx body that was already received
   if len(resp.history) >= self.max_redirects:    # 0 >= 0
       raise TooManyRedirects(f"Exceeded {self.max_redirects} redirects.", response=resp)
   ```
   With `max_redirects = 0`, the condition is true for the first 3xx. The exception is raised before the
   copy of the request is rewritten (`rebuild_method`, `:370-392`) and before the follow-up is sent
   (`:292-301`). **No second request exists.** That applies to every redirect status: 301, 302, 303, 307 and
   308.
4. `KiteConnect._request` re-raises any exception unchanged (`connect.py:915-916`). `TooManyRedirects` is a
   `requests.RequestException`, **not** a `KiteException`. So:
   - `execution._execute_live` / `_execute_exit` order: not in `DEFINITE_REJECTIONS` → `unknown_result`
     → **UNKNOWN — DO NOT RESUBMIT**, and the order monitor searches by tag;
   - `execution._execute_live` protective GTT: `except Exception` → `protection_failed`, `kite_gtt_id=None`.
     This is the existing F9 behaviour (U6§6), unchanged;
   - `gtt_cancellation.cancel`: not in `_DEFINITE_REFUSALS` → the GTT book is always read back. The result
     is `CANCEL_CONFIRMED` / `FAILED` / `OUTCOME_UNKNOWN` from the broker's state, never from the exception;
   - every other caller (routes, loops, Emergency Exit) handles it exactly like the `ReadTimeout` and
     `ConnectionError` it already handles. All are `RequestException`s, and no new handling path was needed.
5. Nothing retries: the transport has `Retry(0, read=False)` (U6§Q2, now pinned by test A2). No application
   path resends a write whose outcome is unknown.

**What the mitigation does not do.** It cannot stop a server from processing the original request before
answering 3xx. That case is the same as any ambiguous answer: the outcome is UNKNOWN until a broker read-back
resolves it. The point of M1 is that the client never sends a **second** copy on its own.

## 3. Tests (M2)

New file: `tests/test_u6_redirects.py`, **136 tests**, all offline.

**Network boundary**

- Loopback only (`127.0.0.1`).
- The upstream host names are `*.invalid`, which can never resolve.
- The `no_direct_sockets` fixture refuses and records any connect beyond loopback.
- The isolation guard reports `network refusals: 0` for the file.

### A. Factory configuration (6 tests)

| Test | Asserts |
|---|---|
| A1 | All four client kinds have `reqsession.max_redirects == 0`: proxied, direct, proxied + writes disabled, direct + writes disabled. The classes are unchanged (`KiteConnect`, `DirectRouteKiteConnect`, `ReadOnlyKiteConnect`). |
| A2 | Each client mounts exactly the two adapters `requests.Session` mounts itself (`https://`, `http://`). Each is exactly `requests.adapters.HTTPAdapter` (no subclass and no SDK `pool`), with `max_retries.total == 0`, `max_retries.read is False`, no `status_forcelist`, and `connect`/`redirect` unset. The session has no response hooks. |
| A3 | The proxied client keeps NET-1 and Phase 2: the proxy mapping, `trust_env False`, the write fence installed, root `https://api.kite.trade`, timeout 7, TLS verification on. |
| A4 | Every client the application obtains, through `get_kite`, `require_kite`, `build_login_url` and `exchange_request_token`, comes from the factory with `max_redirects == 0` and `trust_env False`. |
| A5 | A stand-in `KiteConnect` (demo or test double) is left alone. |
| A6 | Source audit of `backend/`:<br>• no `requests` adapter mount, `HTTPAdapter`, `Retry(`, `max_retries`, tenacity or backoff;<br>• `max_redirects` appears only in `kite_client.py`;<br>• no `allow_redirects=True`;<br>• `reqsession.*` is assigned only in `kite_client.py`;<br>• both `KiteConnect(...)` calls pass only `api_key` / `proxies` (no `pool`, `timeout`, `root` or `disable_ssl`);<br>• the literal M1 line is present. |

### B. Real requests on real sockets (98 tests)

**Harness**

- `LocalKiteProxy` is a real `ThreadingHTTPServer` on `127.0.0.1:0`, configured as the client's
  `KITE_REST_PROXY`.
- The client is a **real factory client**: proxied, fenced, `trust_env False`, with process write authority
  granted by `owner_process`. Its root is set to `http://kite-origin.invalid`, so TLS is not involved.
- Every request the client makes therefore arrives at the server as an absolute-URI proxy request. That
  covers the original and any redirect follow-up, including one to the redirect destination. The server's
  log is the complete record of what was sent.
- The server answers the original with the chosen status and `Location: http://kite-origin.invalid/redirected/landing`.
  This is a same-host redirect, the worst case, because requests keeps the `Authorization` header. It answers
  the redirect destination with 200.

**The matrix:** 7 write calls × 5 statuses (301, 302, 303, 307, 308) = 35 cases.

- `place_order`, `place_gtt` and `generate_session` (POST);
- `modify_order` and `modify_gtt` (PUT);
- `cancel_order` and `delete_gtt` (DELETE).

The six cases the task names (POST 307/308, PUT 301/307, DELETE 301/308) are part of the matrix; a
module-level assertion checks that.

| Test | Cases | Asserts | Result |
|---|---|---|---|
| **B1** redirected write | 35 | `TooManyRedirects` is raised, carrying the 3xx status and `Location`. It is a `RequestException`; not a `KiteException`, not in `execution.DEFINITE_REJECTIONS`, not in `gtt_cancellation._DEFINITE_REFUSALS`. **Exactly one** request reached the server: the original method and URL. **No** request to the redirect destination. The body was sent once, with the original, and **never resent** (DELETE: no body at all). The fence's in-flight counter is back to 0. | 35 passed |
| **B2** no follow-up GET | 15 (POST/PUT/DELETE × 5) | The only method the server saw is the original. No `GET` was ever sent, including for 302, 303, and 301 on POST. | 15 passed |
| **B3** normal response | 7 | A 200 answer is returned to the caller normally, after exactly one request. | 7 passed |
| **B4** redirected read | 5 | A `GET` (`holdings`) answered with a 3xx raises `TooManyRedirects` after one request (see §5). | 5 passed |
| **B5** positive control | 35 | The same harness, with requests' default (30) restored on one client. The server receives the second request M1 prevents, exactly as U6§1 predicted:<br>• 307/308 → the same method, **the same body**, and the same `Authorization: token api_key:access_token`;<br>• 301 on PUT/DELETE → the same method, no body;<br>• 302/303, and 301 on POST → a body-less `GET`. | 35 passed |
| **B6** positive control | 1 | Without M1, a 307 on `place_order` returns the **follow-up's** order id to the caller, which never sees the 307. | passed |

B5 and B6 prove that the harness detects a resend. B1 and B2 are therefore evidence of its absence, not
of a blind spot.

### C. Broker-write integration (32 tests)

The redirect exception is the one the SDK raises under M1: `TooManyRedirects` with the 3xx response.

**FakeKite cases.** These run on the real routes, the real order path (frozen `execution.py`, unchanged), the
order monitor and reconciliation. Most are parametrised over "Kite did not process it" (`raise_on_next`) and
"Kite processed it, then the client saw the redirect" (`accept_then_raise`).

| Test | Asserts |
|---|---|
| C1 entry order | The response is UNKNOWN — DO NOT RESUBMIT, and the message names `TooManyRedirects`. A second confirm returns 409. After 2 monitor + reconciliation cycles: still `unknown` (not processed), or `adopted` by tag (processed). Exactly **1** `place_order`, and **no** protective GTT created by itself. |
| C2 exit order | UNKNOWN; a second confirm returns 409. After 2 cycles: exactly **1** SELL, and the position is still open. |
| C3 protective GTT | `executed` with `protection_failed` (`UNPROTECTED POSITION`), `kite_gtt_id` None. After a fill and 2 cycles: exactly **1** `place_gtt`, and the broker GTT book holds 0 (not processed) or 1 (processed, untracked: F9) GTTs, never 2. |
| C4 GTT delete | `gtt_cancellation.cancel` reads back and classifies from the broker (`CANCEL_FAILED` while still active, `CANCEL_CONFIRMED` when deleted), with the exception kept. Exactly **1** `delete_gtt`. |

**Real client over a recorded transport.** The real `get_kite` builds the real factory client (M1, fence,
proxy). `RedirectingWire` replaces only `HTTPAdapter.send`, so requests' real `Session.send` and
`resolve_redirects` run.

| Test | Asserts |
|---|---|
| C5 entry, 307/308 (2) | A live confirm through the route ends UNKNOWN; a second confirm returns 409. The wire saw exactly one `POST /orders/regular` and nothing to the redirect path. Every send was proxied to `api.kite.trade`. |
| C6 protective GTT, 307 | The entry is accepted, then the GTT is redirected: `protection_failed`. The wire saw exactly `POST /orders/regular` and `POST /gtt/triggers`, once each, and no follow-up. |
| C7 GTT delete, 308 | Exactly one `DELETE`, then one `GET /gtt/triggers` read-back. The outcome is `CANCEL_FAILED` (the broker still lists it active). |
| C8 fence (7) | With write authority lost, every write to a redirecting endpoint is refused with `InstanceNotOwner` and **nothing** reaches the wire. |
| C9 narrowed clients (12) | The direct route (`DirectRouteWritesRefused`) and writes disabled (`BrokerWritesDisabled`) still refuse each write locally, and nothing is sent. Both clients have `max_redirects == 0`. |
| C10 | `LIVE_ENTRIES_ENABLED = False` in the source, and the factory still returns every client through `fence_broker`. |

**No new retry behaviour.** No production code path was added or changed apart from the M1 setting. The
tests assert single sends throughout.

### Mutation check

Before the full regression, `backend/kite_client.py` was temporarily replaced by its pre-edit copy (no M1):
**74 of the 136 tests failed.**

| Group | Failed |
|---|---|
| A1, A4, A6 | 3 |
| B1 | 35 / 35 |
| B2 | 15 / 15 |
| B4 | 5 / 5 |
| C5–C7 | 4 |
| C9 (on the `max_redirects` assertion) | 12 |

These passed, as designed:

- the FakeKite tests C1–C4, which test the handling of the exception, not the transport;
- the positive controls B5 and B6;
- the configuration-invariant tests.

The edited file was then restored, and its sha256 (`adf3b222…6de9`) re-verified against the saved post-edit
copy.

## 4. Regression

All runs from the repository root with `.venv/Scripts/python.exe -m pytest -p no:cacheprovider`, sequentially
(no xdist). Nothing else ran at the same time.

| # | Run | Result |
|---|---|---|
| 1 | Focused: `tests/test_u6_redirects.py` | **136 passed**, 0 failed (67.8 s). Guard: `network refusals: 0`, `unexpected 0`. |
| 2 | Kite client and proxy: `test_kite_rest_proxy.py`, `test_proxy_mandatory.py`, `test_broker_write_fence.py`, `test_broker_writes_switch.py` | **160 passed**, 0 failed (31.3 s) |
| 3 | Order and GTT writes: `test_order_path_assurance.py`, `test_protection_states.py`, `test_exit_lifecycle.py`, `test_gtt_cancellation.py`, `test_partial_fills.py`, `test_live_safety.py`, `test_gtt_protection_truth.py`, `test_emergency_exit_reconciliation.py`, `test_live_entry_gate.py`, `test_margin_preflight.py`, `test_startup_reconciliation.py`, `test_write_authority_routes.py` | **459 passed**, 0 failed (185.4 s) |
| 4 | Full regression (below) | **3566 passed, 0 failed, 0 errors, 6 skipped**; exit code 0 (779.4 s) |

**Full regression command:** `.venv/Scripts/python.exe -m pytest -rs -rf -p no:cacheprovider -o faulthandler_timeout=300 --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py`

**Result: 3566 passed, 0 failed, 0 errors, 6 skipped. Exit code 0, in 779.40 s.**

- **Collected:** 3572 tests, which is Phase 2's 3436 plus the 136 new ones.
- **Skips:** the same 6 as Phase 2, confirmed with `-rs` on their two files (105 passed, 6 skipped). The
  `-rf` given after `-rs` replaces the skip summary in the full run.
  - the 3 known `tests/test_dmb_inbox.py:384` symlink skips (`WinError 1314`);
  - the 3 `WIN-INT PENDING` placeholders: `tests/test_instance_lock.py:194` (L3-subst), `:233` (L6 real
    share), `:674` (R8 real sleep).
- **Isolation guard summary:** identical to Phase 2's final run.
  ```
  python-dotenv neutralised: True; implicit loads neutralised: 5; explicit loads delegated (temporary files): 5; explicit protected loads refused: 2
  inherited KITE_* variables scrubbed at start: 0
  guard refusals: 19 (decoy 15, expected 4, unexpected 0); network refusals: 1
  ```
  The single network refusal is the deliberate TEST-NET-1 test.

**Exclusions:** only the two standing DMB interpreter files that the user approved in Phase 2
(PHASE_2_IMPLEMENTATION_REPORT §1, §9.4), with the same command. They were not run. Nothing else was
deselected.

## 5. GET compatibility

`max_redirects` is a session setting, so it applies to reads too. Kite's GETs no longer follow a 3xx. Each one
fails with `TooManyRedirects` after one request (test B4).

**Reads the application makes through the factory client:**

- JSON: `profile`, `margins`, `holdings`, `positions`, `orders`, `order_history`, `order_trades`, `trades`,
  `get_gtts`, `ltp`, `quote`, `ohlc`, `historical_data`;
- CSV: `instruments("NSE")`.

All go to `api.kite.trade`.

| Question | Answer |
|---|---|
| Does any Kite read depend on a redirect? | **[U]**, not observable offline. Kite does not document a redirect on any REST endpoint, including `GET /instruments/{exchange}`. Before M1, requests would have followed one silently, so the application's history does not prove the absence either. |
| If Kite (or its edge) ever redirects a read, what happens now? | It fails loudly, exactly like a `ReadTimeout`: a `RequestException` on the same handling paths (broker read failure, the circuit breaker, Live Readiness not ready, Stage 1 `BROKER_READ_FAILED`, a route error). It never fails silently, and no wrong data is used. |
| Instrument master (`instruments`) | The exception is raised before `DELETE FROM instrument_cache`, so the existing cache is kept. The startup and post-login refreshes catch and log it (`main.py:155-160`, `routes/auth.py:89-95`). `POST /api/instruments/refresh` returns an error, as it does on a timeout today. |
| Login | The browser login (`login_url()` → `kite.zerodha.com`) is a browser redirect and is unaffected. The token exchange `POST /session/token` would fail on a 3xx, and the login would be retried by the user. A POST should not be redirected in any case. |
| Unaffected | KiteTicker (WebSocket; not built by the factory). The outbound-IP check (`live_preflight.py:255-258`) and the NSE calendar monitor (`nse_calendar_monitor.py:46-49`) use their own sessions, which already set `allow_redirects=False`. |

**Assessment:** acceptable, with no unexpected compatibility problem found. The cost is availability, and
only if Kite starts redirecting: a read fails instead of following. If that ever appears in the field, the
right response is a read-only option for GETs (U6§8 option (b), a response hook that raises only for
non-GET 3xx). It must not reopen redirects for writes.

## 6. Ambiguous outcomes remain UNKNOWN

| Write | On a redirect (now `TooManyRedirects`) | Resubmitted automatically? | Evidence |
|---|---|---|---|
| Entry order | `submission_unknown` → UNKNOWN — DO NOT RESUBMIT; the order monitor looks for the tag | No; a second confirm returns 409 | C1, C5 |
| Exit order | UNKNOWN; the position stays open | No | C2 |
| Protective GTT (frozen `execution.py`) | `protection_failed`, `kite_gtt_id=None` (F9, unchanged) | No; exactly one `place_gtt` | C3, C6 |
| GTT cancel | Broker read-back decides; never inferred from the exception | No; exactly one `delete_gtt` | C4, C7 |
| Order cancel, GTT modify (trailing, resize) | The exception propagates exactly like a timeout | No: the existing paths (U6§Q7) recompute and re-read the broker state before a later absolute-state `PUT`; that behaviour is unchanged | U6§Q7; regression run 3 |

## 7. Confirmations

| Check | Result |
|---|---|
| External network | None. All new tests use loopback or a recorded transport, plus `*.invalid` names that cannot resolve. `no_direct_sockets` recorded no non-loopback connect. The isolation guard reported `network refusals: 0` for the focused file. In the full run, the one network refusal is the deliberate TEST-NET-1 test, as in Phase 2. |
| Broker | No Kite request, no Kite MCP tool, no broker API. |
| `.env`, credentials, account DB, backups | Not opened, read or copied. Size and mtime of all 43 protected files are identical before and after. |
| Dependencies | Nothing installed or upgraded. No third-party file modified. |
| Frozen files | `backend/execution.py` identical to HEAD; PK-25 13 / 13 match. |

## 8. Remaining U6 questions (infrastructure and broker)

M1 removes the only client-side resend path (C1 in U6§7). What remains open is no longer a replay blocker
for the client, but these are still inputs to Phase 3 and to live trading:

1. **Squid** (VM owner): `squid -v`, confirmation that `ssl_bump` is absent, the CONNECT-only `http_access`
   rule, and `connect_retries` / `forward_max_tries` / `retry_on_error`. Expected to be irrelevant to
   replay: it is a CONNECT tunnel with end-to-end TLS, and interception fails closed (U6§Q1). Unverified.
2. **Kite 3xx on writes:** if Kite ever redirects a write, every such write now ends UNKNOWN rather than
   being resent. That is safe, but it is an availability cost, so it is worth asking.
3. **Kite's edge retrying to its OMS:** server-side, outside any client control. **[U]**
4. **Idempotency:** no idempotency key; whether a repeated `tag` is deduplicated is unknown (assume not);
   `place_gtt` carries no client reference.
5. **5xx semantics:** whether an order or GTT may exist after `NetworkException` / `GeneralException`, and
   whether `OrderException` can ever accompany partial processing (U6 F10).
6. **Repeated `modify_gtt` / `delete_gtt`:** no-op or error? This matters for D§4.2's exposure-reducing
   retry class.
7. **Dependency pinning (optional M2 item, not done):** `requests` and `urllib3` are still unpinned in
   `requirements.txt`; changing dependencies was out of scope. An upgrade that changed the relevant
   behaviour would now fail A2, A6, B1, B2 or B5, rather than pass silently.

## 9. Status

### **U6 mitigation: PASS**

- M1 is implemented on every client the factory builds.
- M2:
  - the factory policy is pinned (A1–A6);
  - real-socket tests show one request, no resend and no follow-up GET for every write method and every
    redirect status (B1–B4);
  - a positive control proves the harness would detect a resend (B5, B6);
  - broker-write integration shows conservative handling and no resubmission (C1–C10).
- The mutation check is caught (74 failures).
- The full regression passes: 3566 passed, 0 failed, 6 known skips.
- Per U6§10, U6 is closed on the client side; §8 items 1–6 become documentation and design inputs.

### **Phase 3 readiness: NO-GO**

U6 is no longer a blocker. Phase 3 stays NO-GO until these other prerequisites
(PHASE_2_IMPLEMENTATION_REPORT §13) are resolved:

1. **WIN-INT pending:** L3 `subst` drive alias, L6 real network share, R8 real sleep/hibernate (§10 of
   that report).
2. **U1** DDPI/TPIN for GTT-triggered CNC sells.
3. The D§11.2 dark-schema amendments (Q1–Q3), and renumbering the dark `_m011_*`/`_r011_*` functions to
   v12 before registering the journal. Not started here, as instructed.
4. P§10 items 3, 4, 5 and 9 remain live-trading blockers.

The Phase 3 design must also carry M3 (U6§8): GTT create has no tag, so an ambiguous create is resolved
only by a GTT-book read-back by content, and is never resent before that read-back proves the first attempt
absent (F9, D§4.2 `kind='create' ⇒ attempts ≤ 1`).

**Live trading: NO-GO.** `LIVE_ENTRIES_ENABLED = False`.

## 10. Final integrity checks

Run after the full regression with a read-only scratchpad script (`integrity.py`) that compares
`after.json` with `before.json`. Protected files are checked by `os.stat` only.

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged |
| 2 | Pre-existing worktree entries | All 96 original `git status --short` lines are present with the same codes, and none is missing. Exactly 2 lines were added: `?? docs/U6_MITIGATION_REPORT.md` and `?? tests/test_u6_redirects.py`. The ignored-inclusive listing went from 147 to 149 lines, the same 2 additions. |
| 3 | Content changes | Of the 301 hashed files, exactly one changed: `backend/kite_client.py` (already ` M` before; +17 / −1 vs the pre-edit copy, CRLF kept, no mixed endings). Every other file is byte-identical. New files: the report and the test file. |
| 4 | `backend/execution.py` | identical to HEAD |
| 5 | PK-25 frozen hashes | **13 / 13 match** |
| 6 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, not edited) |
| 7 | `.env` / account DB / backups | Never opened. Size and mtime of all 43 protected files are identical. No new `kite_terminal.db*` file, so the real instance-lock file was not created. |
| 8 | Broker calls / external network | None (§7) |
| 9 | Staging / commits / pushes / stash | Nothing staged (`git diff --cached` empty), no commit, push or merge, `git stash list` empty. No reset, clean, checkout or restore. |
| 10 | Temporary mutation | `kite_client.py` was swapped for its pre-edit copy for one test run (§3), then restored. The sha256 was re-verified before the regression runs. |
