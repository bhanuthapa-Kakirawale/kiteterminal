# NET-1 — Kite REST through the static-IP proxy

Branch `feature/signal-detail-and-strategy-revision`, on top of `a1b80f0`
(blueprint) and `ef6ac53` (LIVE-0). Backups: tag `backup-pre-net1`, branch
`backup/pre-net1`, DB snapshot `backend/kite_terminal.db.pre_net1_bak`
(sqlite backup API, `integrity_check = ok`, 37 tables).

# Executive Summary

Every Kite **REST** call now goes through one explicit, validated proxy
setting, `KITE_REST_PROXY`: login token exchange, profile and portfolio
reads, quotes/LTP, historical data, orders and GTTs.
KiteTicker's WebSocket stays **direct**. No process-wide `HTTP_PROXY` /
`HTTPS_PROXY` is set.

- **One factory.** `kite_client.create_kite_connect` is the only place a
  `KiteConnect` is built. A structural test enforces this.
- **Fail closed.**
  - A malformed value refuses to start and builds no client.
  - A configured proxy that fails raises. It is never retried directly.
  - A failed order submission takes the existing PK-2 UNKNOWN path.
- **Live Readiness.** A new **Kite REST Route** check (`KITE_REST_ROUTE`)
  proves the Windows → proxy → Kite HTTPS route with the existing read-only
  LIVE-0 snapshot. When the proxy is unreachable it blocks READY, even with
  the static IP confirmed.
- **No order-safety change.** Atomic confirmation, UNKNOWN handling, tags,
  partial fills, GTT lifecycle, order monitor, reconciliation, risk,
  strategies, PK-21 and PK-23..26 are unchanged.

**Result:**

- Full suite: **1,732 passed ×3**. The baseline was 1,654, and NET-1 adds
  78 tests.
- 11 of 11 mutation checks were caught, and every mutated file was
  hash-verified after restore.
- The real read-only Kite check through the proxy passes:
  - login (`generate_session`) works through the proxy;
  - profile, holdings, positions, margins, orders, GTTs and LTP all load;
  - KITE_REST_ROUTE = PASS;
  - the outbound IP via the proxy is `35.184.97.233`.
- KiteTicker connected directly.
- No order or GTT was sent.

# Architecture

```
Windows Kite Terminal (127.0.0.1:8000, unchanged)
   │
   ├── Kite REST  ── KiteConnect(proxies={"http": P, "https": P}), trust_env=False
   │                  P = KITE_REST_PROXY = http://10.77.0.1:3128
   │      → WireGuard (Windows 10.77.0.2 → VM 10.77.0.1)
   │      → Squid :3128 (HTTP CONNECT tunnel; TLS stays end-to-end to Kite)
   │      → GCP static IPv4 → api.kite.trade
   │
   └── KiteTicker WebSocket → Kite directly (never proxied)
```

WireGuard and Squid configuration were not touched.

# Why REST Proxy Instead of Full Tunnel

- **Only REST needs the static IP.** Kite's whitelist applies to API order
  calls. A full tunnel would also carry the tick WebSocket, browser traffic
  and every other process through the VM, adding latency and a single
  point of failure to market data without any benefit.
- **Explicit and observable.** The proxy is attached to KiteConnect
  objects only. There is no OS routing change and no global environment
  variable that other libraries (or KiteTicker's Twisted/autobahn stack)
  might also pick up. Readiness can name the route it is using.
- **One client for reads and writes.** All REST calls, reads included,
  share one client. Keeping separate read/write clients risks exactly the
  bug the packet warns about: a mutation built on the wrong client.

# Configuration

| Value | Behaviour |
|---|---|
| unset / blank | Direct (previous behaviour, unchanged). |
| `http://host:port`, `https://host:port` (optionally a trailing `/`) | Proxy for every Kite REST call. |
| anything else | `KiteRestProxyError`. No client is built, and the launcher refuses to start (`exit 2`). |

The following values are rejected:

- a missing scheme (`10.77.0.1:3128`, `//host:3128`);
- `ftp://`, `socks5://`, `ws://`, `file://`;
- an empty hostname;
- a missing, non-numeric or out-of-range port (`0`, `65536`, `abc`);
- **any credentials** (`user:pw@`);
- a path, query or fragment;
- embedded whitespace.

Error messages never echo the raw value. Logs, the API and the UI show
only `host:port`.

Production code holds no IP or port: `10.77.0.1`, `3128` and
`35.184.97.233` appear only in documentation and tests.

`.env.example` documents the variable (commented out). The real `.env` was
**not** edited.

# KiteConnect Integration

The installed pykiteconnect 5.0.1 constructor takes
`proxies=None`. `_request` passes `proxies=self.proxies` to every
`reqsession.request(...)`. It has no internal retry (`except Exception as e: raise e`),
and requests' `HTTPAdapter` defaults to `max_retries=0`.

`backend/kite_client.py`:

```python
def create_kite_connect(api_key):
    proxy = kite_rest_proxy.from_env()          # raises on an invalid value
    if proxy is None:
        return KiteConnect(api_key=api_key)     # direct, unchanged
    kite = KiteConnect(api_key=api_key, proxies=proxy.requests_proxies)
    kite.reqsession.trust_env = False           # env HTTP(S)_PROXY / NO_PROXY / netrc ignored
    return kite
```

There were exactly three `KiteConnect(` constructions, all in
`kite_client.py`: `build_login_url`, `exchange_request_token` and
`get_kite`. All three now use the factory. Every route, worker, preflight
and CLI obtains its client from `get_kite` / `require_kite` /
`require_trading_account`. The other `from kiteconnect import KiteConnect`
lines are type hints only.

`require_kite` turns an invalid proxy into HTTP 503
`KITE_REST_PROXY_INVALID`, never a direct client.

The startup log (`server.enforce_runtime_policy`) prints either
`Kite REST proxy enabled: 10.77.0.1:3128` or
`Kite REST: direct (KITE_REST_PROXY not set)`.

# KiteTicker Isolation

`backend/ticker.py` is **unchanged**: `KiteTicker(api_key, access_token)`
and `connect(threaded=True)`, with no proxy argument. Tests prove three
things:

- With `KITE_REST_PROXY` set (http or https), a recording `KiteTicker` is
  built with exactly `("key", "tok")`, no keyword arguments, and connects
  with `threaded=True` only. This holds on restart with a new token too.
- `ticker.py` never mentions a proxy, `kite_rest_proxy` or
  `create_kite_connect`. Its only `KiteTicker(...)` call has two positional
  arguments and no keywords.
- No other backend file constructs a `KiteTicker`.

# Login / Session Routing

`exchange_request_token` builds its client through the factory, so
`generate_session` (POST `/session/token`) goes through the proxy. The
test records that exact POST, carrying the proxy mapping, at the HTTP
adapter boundary. `set_access_token` only sets an attribute (no request).
`login_url()` is local string building, but its client is also built with
the proxy.

# Read-Only REST Routing

The tests use a real KiteConnect with `HTTPAdapter.send` replaced by a
recorder. These calls all reach the adapter with
`proxies == {"http": P, "https": P}` and host `api.kite.trade`: `profile`,
`holdings`, `positions`, `margins`, `orders`, `get_gtts`, `ltp`, `quote`,
`historical_data`, and the `/api/holdings` route end to end. They still
do with `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and
`NO_PROXY=api.kite.trade,*` set in the environment.

# Order / GTT Routing

Through `require_trading_account(get_kite())`, the same proxied client
object sends `place_order` (POST `/orders/regular`), `modify_order` (PUT),
`cancel_order` (DELETE), `place_gtt` (POST `/gtt/triggers`), `modify_gtt`
(PUT) and `delete_gtt` (DELETE), each through the proxy. A test asserts
that the trading dependency returns the very same client object. All of
this runs against the recorder: no real order or GTT was sent.

# Fail-Closed Behaviour

- **Proxy failure on a call.** A ProxyError, ConnectTimeout or ReadTimeout
  at the adapter is raised to the caller after exactly one send, which
  carries the proxy. There is no second send and no second client.
- **Invalid configuration.** `get_kite` raises before any KiteConnect
  exists, `require_kite` returns 503, `/api/holdings` returns 503, and
  nothing is sent.
- **Proxy dies during order submission.** FakeKite's `place_order` raises
  `requests` ProxyError, ConnectTimeout or ReadTimeout. The confirm
  returns UNKNOWN ("DO NOT RESUBMIT"), the ticket's `submission_state` is
  `unknown`, and `place_order` was called once (`execution.place_trade`'s
  existing `except Exception → unknown_result`).
- **Accepted, then lost.** The order is accepted and the response is lost
  through the proxy. Result: UNKNOWN, a second confirm returns 409, and
  `place_order` was still called once.
- **Proxy dies before the order.** The pre-order risk revalidation reads
  fail, so the confirm returns 503 `RISK_REVALIDATION_UNAVAILABLE`.
  `place_order`/`place_gtt` are never called, and the failure is recorded
  for the PK-12G broker circuit breaker
  (`routes/tickets._is_broker_failure` already counts
  `requests.RequestException`).

No order-safety code was changed. The proxy sits underneath it.

# LIVE Readiness Integration

New section **Kite REST Route** (`network`, after Broker), check
`KITE_REST_ROUTE`:

| Route | Snapshot | Status | Blocking |
|---|---|---|---|
| INVALID | — | FAIL (no broker call made) | yes |
| DIRECT | — | WARN "No REST proxy configured…" | no (backward compatible) |
| PROXY | none (broker not checked) | ACTION REQUIRED "Run the broker check…" | yes |
| PROXY | no session | ACTION REQUIRED (log in; login also uses the proxy) | yes |
| PROXY | Kite answered (data, or a Kite API error) | **PASS** "Kite REST via proxy host:port: read-only Kite calls answered at … UTC" | yes |
| PROXY | every read failed in transport | **FAIL** "…proxy route is unreachable (…). Calls are never retried directly" | yes |

- A confirmed static IP therefore cannot make the page READY while a
  configured proxy is unreachable.
- `evaluate()` also returns `rest_route: {mode, endpoint}`, and the page
  header shows `REST via PROXY 10.77.0.1:3128` / `REST DIRECT`.
- `BrokerSnapshot.transport_errors` records which reads failed with a
  `requests` exception. That is how the check tells "the proxy is down"
  apart from "Kite rejected the session".
- The confirm-time gate (`account_safety.check_live_prerequisites`) is
  unchanged.

# Security

- The proxy accepts no credentials, and none can reach logs, the API or
  the UI. Readiness payloads were checked for the access token, API
  secret, proxy user and password, and `Authorization`.
- No global proxy environment variable is set anywhere in `backend/`. A
  test scans for environment writes.
- **Squid** listens on `10.77.0.1` only, and its source ACL allows
  `10.77.0.2` only. **TCP 3128 must never be opened in the GCP firewall**:
  only the WireGuard UDP port is public.
- The app stays bound to `127.0.0.1`. `KITE_TERMINAL_ALLOW_REMOTE_BIND` is
  not needed, and server binding defaults are unchanged.
- `docs/DEPLOYMENT.md` has a new "Mode A+" section covering this.

# Proxy Health Check

No TCP probe and no ipify on refresh. The check reuses the one LIVE-0
read-only snapshot (profile, holdings, positions, margins, orders, GTTs,
one LTP), taken by `get_kite()`: the same factory and route every order
uses. There are no extra broker calls, and the snapshot is still cached
for 30 s.

**Manual outbound-IP diagnostic.** The existing "Check this machine's
outbound IP" button would have seen the home ISP IP and turned STATIC_IP
into FAILED. When a proxy is configured, it now sends its one request
through the same proxy, with the same mechanism as the KiteConnect client:
`requests` with `trust_env` off and explicit `proxies` (since `57d9cd2`;
the earlier `urllib` `ProxyHandler` still honoured `NO_PROXY` and the
Windows proxy-bypass list). Since 2026-11-05 a VERIFIED observation taken
through the configured proxy, by the running process, is required to arm
LIVE, and the proxy itself is mandatory for any broker write (the direct
route is read-only). The button is labelled "Verify outbound IP via the Kite
REST proxy (host:port)". It stays user-triggered, read-only toward Kite,
and separate from the readiness refresh.

# Tests

`tests/test_kite_rest_proxy.py` has 75 tests. `tests/test_live_readiness_ui.py`
gains 3. `tests/test_live_preflight.py` has one assertion updated: the
section set now includes `network`.

| # | Requirement | Test(s) |
|---|---|---|
| 1 | unset → direct | `test_01` |
| 2 | blank → direct | `test_02` (3 cases) |
| 3 | http → exact mapping, `trust_env` off | `test_03` |
| 4 | https accepted (and normalised forms, IPv6) | `test_04` |
| 5 | malformed rejected | `test_05` (11 cases) |
| 6 | unsupported scheme rejected | `test_06` (4) |
| 7 | invalid port rejected | `test_07` (5) |
| 8 | sanitized logs/status; credentials refused, never echoed; launcher exit 2 | `test_08`, `08b`, `08c`, `08d` |
| 9 | profile via proxy | `test_09` |
| 10 | holdings (and every read) via the same client; `/api/holdings` end to end | `test_10`, `10b` |
| 11 | order placement / modify / cancel via the same client | `test_11`, `11b` |
| 12 | GTT place / modify / delete via the same client | `test_12` |
| 13 | historical data via the same client | `test_13` |
| 14 | login token exchange and login URL via the proxy; env proxies cannot bypass; factory is the only constructor; no global proxy env writes | `test_14`, `14b`–`14e` |
| 15 | KiteTicker construction unchanged | `test_15` (3) |
| 16 | KiteTicker never receives or references the proxy | `test_16` |
| 17 | proxy failure not retried directly; invalid → no client, 503 | `test_17` (3), `17b` |
| 18 | proxy timeout on place_order → UNKNOWN | `test_18` (3) |
| 19 | accepted-then-lost → one call, no duplicate; proxy dies in risk revalidation → no order, breaker counted; wire-level one send | `test_19`, `19c` (2), `19b` |
| 20 | unreachable proxy → NOT READY despite static IP; invalid → FAIL, no broker call; not proven → ACTION; Kite API error ≠ route failure | `test_20`, `20b`–`20d` |
| 21 | healthy proxy → PASS and READY; no proxy → WARN, READY unchanged | `test_21`, `21b` |
| 22 | broker check reports PROXY | `test_22` |
| 23 | no token/secret/credentials in readiness; outbound-IP check uses the proxy (and stays direct without one) | `test_23` (2), `23b`, `23c` |
| 24 | DRY RUN works with a proxy, no broker mutation | `test_24` |
| 25 | direct mode backward compatible | `test_25` and the full pre-existing suite |
| UI | route in the header, own section, proxy-aware outbound-IP button | `test_net1_*` (3) |

The routing tests use a real KiteConnect, with `requests.adapters.HTTPAdapter.send`
replaced by a recorder, so the proxies asserted are exactly what requests
hands to the connection layer. `tests/conftest.py` clears `KITE_REST_PROXY`
for every test (autouse), so a real `.env` value can never leak into the
suite through `load_dotenv()`.

**Repetition:**

- NET-1 targeted (`test_kite_rest_proxy`, `test_live_readiness_ui`,
  `test_live_preflight`) ×10: **167 passed** every run.
- Fail-closed subset (`test_17`–`test_20`) ×20: **15 passed** every run.
- KiteTicker isolation subset (`test_15`, `test_16`) ×20: **4 passed**
  every run.

# Mutation Results

Each mutation is a temporary edit. The targeted tests were run against it,
then the file was restored byte for byte and its sha256 verified.

| # | Mutation | Result |
|---|---|---|
| 1 | `proxies=` dropped in `create_kite_connect` | CAUGHT (8 failed) |
| 2 | trading dependency returns a fresh direct KiteConnect, so place_order/GTT bypass the proxy | CAUGHT (4 failed) |
| 3a | invalid proxy silently falls back to direct | CAUGHT (2 failed) |
| 3b | proxy failure retried directly | CAUGHT (4 failed) |
| 4 | KiteTicker `connect(..., proxy=...)` gets the REST proxy | CAUGHT (4 failed) |
| 5 | Live Readiness PASS while the proxy is unreachable | CAUGHT (1 failed) |
| 6 | proxy URL with credentials echoed in the error/API/log | CAUGHT (3 failed) |
| 7a | missing scheme silently accepted | CAUGHT (4 failed) |
| 7b | unsupported scheme silently accepted | CAUGHT (4 failed) |
| 8 | place_order treats transport (proxy) errors as definite rejections, bypassing UNKNOWN | CAUGHT (4 failed) |
| 8b | proxy failure during risk revalidation no longer counted as a broker failure | CAUGHT (2 failed) |

Final hashes after restore (all OK): `execution.py`, `kite_client.py`,
`kite_rest_proxy.py`, `live_preflight.py`, `routes/tickets.py` and
`ticker.py`. `execution.py`, `routes/tickets.py` and `ticker.py` are
unchanged compared to HEAD.

Two findings from the first pass, both fixed before the final run:

- **Mutation 8 was mis-aimed.** The first version mutated
  `tickets._is_broker_failure` and was **missed**. That function only
  classifies risk-revalidation failures for the circuit breaker; the
  place_order UNKNOWN path is `execution.place_trade`'s `except Exception`.
  Mutation 8 was re-aimed at the real handler (caught). The original
  became 8b, and `test_19c` was added to cover it (caught).
- **Mutation 7a hung.** It made `test_08d` start a real uvicorn server, so
  the hung child was stopped. `test_08d` now stubs `uvicorn.run`, so a
  regression fails fast instead of serving.

# Real Read-Only Validation

The server was started with
`KITE_REST_PROXY=http://10.77.0.1:3128` set for that process only (`.env`
not edited), using `python -m backend.server --host 127.0.0.1 --port 8000`.
The startup log reads
`Kite REST proxy enabled: 10.77.0.1:3128`, `bind=127.0.0.1 … remote_exposure=no`.

Manual sanity check: `curl.exe -x http://10.77.0.1:3128 https://api.ipify.org`
returned `35.184.97.233`, and `https://api.kite.trade/` answered HTTP 200
through the proxy.

**Login.** The user logged in from the terminal: `/login` → Kite →
`/callback`. The token exchange went through the proxy and the session
was stored. The first attempt never reached the server, because the
browser did not come back to `127.0.0.1:8000/callback`. The second, started
from the terminal's own Login, worked.

**KiteTicker** connected **directly** to Kite (`peer tcp4:43.204.153.52:443`),
both after login and after restart.

**`GET /api/live-readiness?broker=true&refresh=true`:**

- `rest_route = {mode: PROXY, endpoint: 10.77.0.1:3128}`.
- **KITE_REST_ROUTE PASS** "Kite REST via proxy 10.77.0.1:3128: read-only
  Kite calls answered".
- BROKER_SESSION PASS, ACCOUNT_IDENTITY PASS (masked), and
  BROKER_PAYLOAD_SHAPE PASS (holdings 14, positions 0, margins OK).
- No BROKER_READS failure: orders and GTTs were read too.
- MARKET_DATA_LTP PASS, and BROKER_OPEN_ORDERS_UNLINKED PASS (0 working
  orders).
- The payload contains no access token, API secret or `Authorization`.

**`python -m backend.live_preflight payload-shape`**, through the proxy:
`errors: {}`, `ltp_ok: true`, holdings 14 rows, orders 0, GTTs 0, margins
`commodity`/`equity`, semantics problems none.

**Outbound-IP check** via the proxy (`POST /api/live-readiness/egress-check`):
observed **`35.184.97.233`**. STATIC_IP remains USER_VERIFICATION_REQUIRED
because the static IP was deliberately **not** attested on the user's
behalf.

**No mutation.** The only non-GET request in the run was the egress
check. The `trade_tickets` count and max id (19 / 9257), the
`ticket_events` count (53) and the live broker IDs (0) are identical to
the pre-NET-1 snapshot. Execution mode stayed **DRY RUN** and the trading
lock stayed **ON**. No order or GTT was sent to Kite.

**Restart validation:**

- `KITE_REST_PROXY=10.77.0.1:3128` → `Refusing to start: … the scheme is
  missing`, exit 2.
- `http://user:pw@…` → `… credentials in the proxy URL are not supported`,
  exit 2, credentials not echoed.
- A restart with the valid value logged the proxy line again. The ticker
  reconnected directly and KITE_REST_ROUTE was PASS again.
  RECON_LAST_CYCLE also passed: the reconciliation worker's Kite reads
  succeeded through the proxy.

The remaining NOT_READY blockers are pre-existing and unrelated to NET-1:
STATIC_IP not attested, MAX_ORDER_NOTIONAL / DAILY_LOSS_AMOUNT not set,
STRATEGY_HISTORY_FRESH, and TRADING_LOCK ON.

# Browser Validation

Checked by the user on `http://127.0.0.1:8000/#live-readiness`, after a
hard refresh and **Run broker check (read-only)**. The browser-automation
tool was not available in this session. The user reported **all PASS**:

- the **Kite REST Route** section shows PASS, "Kite REST via proxy
  10.77.0.1:3128 …";
- the header line ends with "· REST via PROXY 10.77.0.1:3128";
- the Static IP panel is present, with "Verify outbound IP via the Kite
  REST proxy (10.77.0.1:3128)" and the observed outbound IP;
- the trading lock is still ON, and no secrets or proxy credentials are
  visible;
- no order was submitted;
- the console is clean, with no CSP violations.

The page code still uses `textContent` only. The static checks for
`innerHTML`, `eval` and inline handlers pass, and the page calls only the
readiness endpoints.

# Full Suite ×3

| Run | Result |
|---|---|
| Baseline (before NET-1) | 1,654 passed |
| 1 | **1,732 passed** (310.7 s) |
| 2 | **1,732 passed** (341.7 s) |
| 3 | **1,732 passed** (381.1 s) |

# Known Limitations

- **Stored outbound-IP observation.** An observation stored *before* the
  proxy was enabled (the home ISP IP) keeps STATIC_IP in FAILED for up to
  24 h. Re-run "Verify outbound IP via the Kite REST proxy" to replace
  it. The observation is not route-tagged in the database, because that
  would need a schema change.
- **PASS proves Kite answered, not the egress IP.** A PASS on the route
  proves that Kite answered through the proxy. It does not prove that the
  proxy's egress IP equals the whitelisted IP: Kite exposes neither. That
  remains the STATIC_IP attestation, optionally VERIFIED by the manual
  outbound-IP check through the proxy.
- **Proxy failure surfaces as transport errors.** If the proxy goes down
  mid-session, the existing transport-failure handling applies: UNKNOWN
  outcomes and circuit-breaker counting. There is no automatic failover,
  by design.
- **Value is read per client build.** `KITE_REST_PROXY` is read each time
  a client is built. Changing it in `.env` needs a restart, which is also
  when it is validated.

# Phase-2 Execution Gateway Deferred

Running order execution on the GCP VM itself (a remote execution gateway)
is intentionally **not** started. The terminal, its database and every
safety system stay on Windows. Only the REST egress moved.

# PK-27 / PK-28 Deferred

Not started.
