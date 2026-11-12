# U6: NET-1 proxy replay investigation

**Status:** **CONDITIONALLY SAFE** (§7). **Phase 3: NO-GO at this HEAD**, and GO once mitigation M1 lands (§10).

This answers open item U6 from `docs/PHASE_2_DESIGN_REVIEW.md` (§2.4 row 8, §13 U6) and
`docs/PHASE_2_IMPLEMENTATION_REPORT.md` §13 item 1: can the NET-1 REST proxy, or any layer under it,
automatically replay a non-idempotent Kite request?

The investigation was read-only:

- Repository: `D:/Claude_WS/kite-terminal`, branch `feature/signal-detail-and-strategy-revision`, HEAD `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`.
- Sources read: the application source and the installed dependency source in `.venv/Lib/site-packages`.
- Not done:
  - no application run and no test run;
  - no broker or external network request;
  - `.env`, credentials and account databases were not opened;
  - nothing was installed.
- The only file written to the repository is this report.

**Evidence labels**

| Label | Meaning |
|---|---|
| **[V]** | Verified in source: this HEAD, or the installed dependency version |
| **[T]** | Verified by an existing test (cited; not re-run here) |
| **[I]** | Inference from verified facts and protocol properties |
| **[U]** | Unknown; needs broker or infrastructure confirmation |

## 1. Executive conclusion

**No transport layer retries after a failure** [V]. A connection failure, timeout or reset at any point in a broker write raises an exception after exactly one send:

- the application wrappers do not retry;
- kiteconnect 5.0.1 does not retry;
- requests 2.34.2 and urllib3 2.7.0 run with `Retry(0, read=False)`;
- Python's `http.client` does not retry;
- an HTTP 5xx response is returned to the SDK once and never resent.

**Squid cannot replay a request** [V for the client side, I for Squid]. Squid runs as a blind HTTP CONNECT tunnel. The client verifies TLS end to end against an unmodified certifi bundle, so Squid sees only ciphertext. It cannot resend a request that a fresh TLS session would accept.

**One automatic resend mechanism exists: redirect following** [V]. The SDK hard-codes `allow_redirects=True` for every method (`connect.py:911`), and requests follows up to 30 redirects. What gets resent depends on the status:

| Status | Methods | What requests resends | Authorization header |
|---|---|---|---|
| 307, 308 | any (`POST /orders`, `POST /gtt/triggers`, …) | the same method and the same body, to the `Location` URL | kept when the host, scheme and port are unchanged |
| 301 | `PUT`, `DELETE` | the same method, without a body | kept, as above |
| 302, 303 (any method); 301 on `POST` | — | a body-less `GET` | — |

Only an endpoint holding a valid `api.kite.trade` certificate can trigger this, meaning Kite itself or its edge, because TLS is verified end to end. Whether Kite ever sends 307 or 308 on a write endpoint is **[U]**.

**Classification: CONDITIONALLY SAFE** (§7). The redirect path is closed by a small client-side change (M1, §8) that needs no broker confirmation.

## 2. Actual request path (broker write, LIVE route)

Example: `execution._execute_live` → `kite.place_order(...)`; every other write follows the same path.

| # | Layer | What happens | Reference |
|---|---|---|---|
| 1 | Caller | One call to `kite.place_order` / `place_gtt` / `modify_gtt` / `delete_gtt` / `cancel_order`, with no loop around it | `execution.py:468, 487, 503, 583`; `gtt_cancellation.py:58`; `position_quantity.py:318`; `trailing_engine.py:813`; `strategy_engine.py:677`; `routes/orders.py:133-135`; `routes/kill_switch.py:76` [V] |
| 2 | SDK method | Builds the parameters, then calls `_post`, `_put` or `_delete`, each of which calls `self._request(...)` once | `connect.py:336-363, 365-385, 387-391, 715-738, 740-763, 765-767, 863-873` [V] |
| 3 | Write fence | `fence_broker` installs `_fenced_request` as an instance attribute. A non-GET runs inside `instance_lock.broker_write(...)`, which either refuses before sending (`InstanceNotOwner`) or counts the call in flight. It runs the class's `_request` once. | `kite_client.py:164-169, 182-195`; `instance_lock.py:649-663` [V] |
| 4 | Route narrowing | With no proxy configured, `DirectRouteKiteConnect`. With writes disabled, `ReadOnlyKiteConnect`. Both refuse every non-GET except `api.token` before sending; otherwise they call `super()._request` once. | `kite_client.py:82-90, 127-135, 198-213` [V] |
| 5 | Client factory (NET-1) | `KiteConnect(api_key, proxies={"http": P, "https": P})` with `reqsession.trust_env = False`. No `pool=`, `timeout=` or `disable_ssl=` is passed, so the SDK defaults apply: the session's default `HTTPAdapter`, a 7 s timeout, and TLS verification on. | `kite_client.py:211-212`; `kite_rest_proxy.py:62-65`; `connect.py:38, 202-214` [V] |
| 6 | `KiteConnect._request` | One `reqsession.request(method, url, data/json, verify=True, allow_redirects=True, timeout=7, proxies=...)`. Any exception is re-raised unchanged (`except Exception as e: raise e`). A JSON error body is mapped to an exception by `error_type`, not by HTTP status. | `connect.py:875-943` (send `904-913`, re-raise `915-916`, mapping `922-937`) [V] |
| 7 | `requests.Session.send` | One `adapter.send(...)`. If `allow_redirects` is set, `resolve_redirects` then follows any 3xx response. | `requests/sessions.py:752-826`, `186-307` [V] |
| 8 | `HTTPAdapter.send` | One `conn.urlopen(..., redirect=False, retries=self.max_retries)`. `max_retries` is `Retry(0, read=False)` by default. Errors become `ConnectionError`, `ProxyError`, `ConnectTimeout`, `ReadTimeout` or `SSLError`. | `requests/adapters.py:81, 205-211, 634-744`; default adapter mounted at `sessions.py:502-503` [V] |
| 9 | urllib3 pool and CONNECT | `_get_conn` discards a pooled connection the OS reports as dropped, before sending. A new connection sends `CONNECT api.kite.trade:443` to the proxy; any non-200 answer raises `OSError("Tunnel connection failed")`, which becomes `ProxyError`. TLS then runs through the tunnel to Kite. | `urllib3/connectionpool.py:288-290, 772-778, 1042-1054`; Python `http/client.py:1008-1010` [V] |
| 10 | TLS | `verify=True` uses certifi's `cacert.pem`. Because `trust_env=False`, `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` are ignored. The installed bundle's sha256 matches its wheel `RECORD` (`nMKndLUZ…cw8`). No trust-store injector (truststore, pip-system-certs) is installed; the only `.pth` file is `pywin32.pth`. | `requests/adapters.py:307-345`; `sessions.py:845-860`; `utils.py:82` [V] |
| 11 | Network | WireGuard `10.77.0.2` → `10.77.0.1:3128` (Squid, HTTP CONNECT) → GCP static IP → `api.kite.trade` | `docs/DEPLOYMENT.md:160-215`; `docs/NET1_KITE_REST_PROXY_REPORT.md:44-58` (documented; Squid configuration is not in this repository) [I] |

## 3. Findings Q1–Q8

### Q1. Can NET-1 automatically replay a non-idempotent request?

NET-1 has two parts.

**The client code** [V]:

- `kite_rest_proxy.py` only parses `KITE_REST_PROXY` into a `proxies` mapping (`:62-65`, `:72-103`).
- `create_kite_connect` only attaches that mapping and disables environment proxies (`kite_client.py:207-213`).
- It contains no retry, no fallback to the direct route, and no second client.
- [T] `test_kite_rest_proxy.py:429-435`: one wire send on `ProxyError`, `ConnectTimeout` and `ReadTimeout`.
- [T] `:500-503` (`test_19b`): one wire send for a failing `place_order`.
- [T] `test_proxy_mandatory.py:166-183`: exactly one `CONNECT` reaches a real local mock proxy, and no direct socket is opened.

**Squid**: it operates as a CONNECT tunnel, and TLS is end to end [V client side, I proxy side]:

- Squid relays opaque TLS records and never holds the plaintext request.
- Any reconnect is a new TCP connection and needs a new TLS handshake with new keys, so stored ciphertext from an earlier session cannot be replayed into it.
- Within one connection, TLS record sequence numbers reject duplicates.
- Python's `ssl` does not send TLS 1.3 0-RTT early data, so the 0-RTT replay window does not exist.

If Squid were configured for TLS interception (`ssl_bump`), every request would fail certificate verification at row 10 instead of being proxied. Interception therefore fails closed rather than silently giving Squid the plaintext.

Squid's own forwarding retries (for example trying another destination address) happen while it builds the tunnel. That is before it answers `200` to the `CONNECT` and before any request byte exists [I; Squid configuration and version are [U]].

**Answer:** No, under the documented deployment. The Squid side rests on documentation, not on configuration review (§9 item 1).

### Q2. Can the HTTP library or the Kite SDK retry independently of NET-1?

**After a failure: no** [V].

- **kiteconnect 5.0.1:** a single `reqsession.request` with no loop. Exceptions are re-raised (`connect.py:904-916`).
- **requests 2.34.2:** `DEFAULT_RETRIES = 0` gives `Retry(0, read=False)` (`adapters.py:81, 208-209`).
- **urllib3 2.7.0, `Retry.increment` with `total=0, read=False`:**
  - a read-class error (`ReadTimeoutError`, `ProtocolError`) re-raises immediately because `read is False` (`retry.py:397-401, 497-498`);
  - any other error takes `total` to −1, `is_exhausted()` is true, and `MaxRetryError` is raised (`retry.py:476-477, 433-450, 541-543`);
  - the pool's "Try again" recursion (`connectionpool.py:866-886`) is therefore unreachable;
  - a status-based retry needs `status_forcelist` or a truthy `total` plus `Retry-After`; neither is present (`retry.py:411-431`, `connectionpool.py:942-943`).
- **urllib3 broken pipe:** a `BrokenPipeError` while sending is swallowed so that a response already sent can still be read (`connectionpool.py:507-508`). That reads, it does not resend.
- **`http.client`:** contains no retry logic; `auto_open` only opens a socket before the first send (`client.py:846, 1065`).
- **The application:** nothing mounts an adapter, sets `max_retries` or `Retry`, or uses tenacity or backoff around Kite. A grep of `backend/` for `max_retries|Retry(|HTTPAdapter|.mount(|tenacity|backoff|reqsession|allow_redirects|max_redirects` finds only `kite_client.py:212` (`trust_env`) and two unrelated non-Kite sessions that already use `allow_redirects=False` (`live_preflight.py:255-258`, `nse_calendar_monitor.py:46-49`).

**Through redirects: yes**, see Q3.

### Q3. Can redirects cause the original request body to be resent?

**Yes, if the Kite endpoint answers 307 or 308** [V code, U trigger].

- `KiteConnect._request` always passes `allow_redirects=True` (`connect.py:911`). The application cannot change that per call.
- `Session.max_redirects` defaults to 30 (`sessions.py:488`, `models.py:104`).
- Inside `resolve_redirects`:
  - the request is copied (`:205`);
  - the method is rewritten only for 302 and 303 (any method → `GET`) and for 301 on `POST` (→ `GET`) (`:370-392`);
  - the body and `Content-Length`/`Content-Type` are dropped only when the status is **not** 307 or 308 (`:250-258`);
  - `Authorization` (which carries `token api_key:access_token`) is stripped only when the host, scheme or port changes (`:154-184`, `:309-329`);
  - the copy is sent with the same proxies and timeout (`:292-301`).
- Consequences:
  - a 307 or 308 on `POST /orders/regular` or `POST /gtt/triggers` resends the same body to the new URL, authenticated if same-host;
  - a 301 on `PUT` or `DELETE` resends that method without a body;
  - urllib3's own redirect handling is off (`redirect=False`, `adapters.py:701`).
- Who can trigger it:
  - not Squid: a non-200 `CONNECT` answer is an error, never a redirect (`client.py:1008-1010`);
  - not a network attacker, because TLS is verified;
  - only Kite's TLS endpoint (its servers or its TLS-terminating edge/WAF) [I].
- An RFC-conforming 307 or 308 means the original was **not** processed, so the resend would be the first execution, not a duplicate. Nothing guarantees that conformance for a broker write, and the application never sees the intermediate response [I].
- No test covers a 3xx answer to a write. The proxy tests replace `HTTPAdapter.send` and only test environment-proxy "redirection" (`test_kite_rest_proxy.py:46-87, 339`) [V].

### Q4. Connection failure at each stage (one attempt; no automatic resend)

| Failure point | Exception reaching the caller | Did Kite receive it? | Automatic resend? |
|---|---|---|---|
| Proxy unreachable, or `CONNECT` refused or timed out | `ProxyError` / `ConnectTimeout` | No: the tunnel never reached Kite [I] | No [V][T] |
| Tunnel up, TLS handshake fails | `SSLError` (wrapped as `ProxyError` if the proxy hop itself failed) | No request bytes were sent [I] | No [V] |
| Stale pooled connection detected before sending | none: urllib3 opens a new connection before any byte is sent (`connectionpool.py:288-290`) | — | It is the first and only send [V] |
| Reset or drop during transmission (incl. a stale-pool race) | `ConnectionError` (`ProtocolError` "Connection aborted") | **Unknown**: possibly partial | No [V] |
| Full request sent, no response (read timeout 7 s, or reset) | `ReadTimeout` / `ConnectionError` | **Unknown** | No [V] |
| Kite processed it, response lost (proxy, tunnel or WireGuard drop) | same as the previous row | **Yes** | No [V]. The SDK cannot distinguish this from the previous row. [T] `test_kite_rest_proxy.py:468-477`: an accepted-then-lost `place_order` gives UNKNOWN; a second confirm returns 409; `place_order` is called once (FakeKite level). |
| HTTP 5xx with a JSON body | Kite exception by `error_type`, e.g. `NetworkException` / `GeneralException` (`connect.py:930-937`) | **Unknown**: `NetworkException` signals a Kite↔OMS problem [U] | No [V]: not in any retry list |
| HTTP 5xx without JSON (an edge/LB HTML page) | `DataException` (`connect.py:942-945`); `KeyError` if `Content-Type` is absent (`:922`) | **Unknown** | No [V] |

TCP-level retransmission inside WireGuard is not an application replay: the receiver deduplicates segments by sequence number [I].

How the application treats these outcomes:

| Call site | Definite failure | Anything else | Reference |
|---|---|---|---|
| `execution._execute_live` / `_execute_exit` entry or exit order | `OrderException`, `InputException`, `PermissionException` → failed | UNKNOWN, "DO NOT RESUBMIT"; `order_monitor` looks for the tag | `execution.py:92, 394-408, 467-478, 582-591` [V] |
| `gtt_cancellation.cancel` | — | always followed by a GTT-book read-back; ambiguous results become `CANCEL_OUTCOME_UNKNOWN` | `gtt_cancellation.py:54-96` [V] |
| `execution._execute_live` **protective GTT** | — | any exception gives `protection_failed` with `kite_gtt_id=None`, although the GTT may exist at Kite (an untracked orphan, see §6) | `execution.py:484-532` [V; frozen file, unchanged] |

### Q5. Are retry policies different for GET and for POST/PUT/DELETE?

**At this HEAD, not at the transport:** zero retries for every method [V].

There are latent and adjacent differences:

- urllib3's `DEFAULT_ALLOWED_METHODS` includes `PUT` and `DELETE` but not `POST` (`retry.py:197-199`). If retries were ever enabled, for example by passing `pool={"max_retries": N}` to `KiteConnect` (`connect.py:212-214`):
  - `modify_order`, `cancel_order`, `modify_gtt` and `delete_gtt` would be retried after read errors;
  - every method would be retried after connect errors.
  - No test pins `max_retries == 0` on the factory's client [V].
- Redirect rewriting depends on the method (Q3).
- The application refuses or fences writes by method: any non-GET is a write (`kite_client.py:86, 131, 166`) [V].

### Q6. Is there an idempotency key, broker request ID or deduplication?

**No mechanism makes a replay safe** [V for the client; U for Kite].

- `_request` sends only `X-Kite-Version`, `User-Agent` and `Authorization` (`connect.py:886-893`). There is no idempotency or request-ID header.
- `place_order` accepts `tag` (`connect.py:352`), and the application sets it (`execution.py:461, 578`). The tag is a correlation label that `order_monitor` uses to find an ambiguous order afterwards. Whether Kite rejects a second order with the same tag is [U]; nothing in the SDK or the application assumes it does.
- `place_gtt` sends only `condition`, `orders` and `type` (`connect.py:680-713, 735-738`). There is no tag and no client reference. An ambiguous GTT create can be matched only by content in `get_gtts()` (P§5.1, D§2.4 row 7).
- Application-level deduplication is real but sits above the transport:
  - ticket claims allow one execute per confirm (a second confirm returns 409, [T] `test_kite_rest_proxy.py:474-476`);
  - one in-flight exit per position (`db.py:964-998`, cited in D§2.5);
  - these prevent the **application** from resending, not a transport replay.
- `modify_gtt`, `cancel_order` and `delete_gtt` set or remove an absolute state, so resending identical content should converge [I]. Kite's semantics for a repeated modify or delete are [U].

### Q7. Does a proxy restart, application restart or reconnect replay an in-flight request?

**No** [V client; I proxy].

- **Proxy, Squid or WireGuard restart:** the in-flight request fails as a row in Q4. A new tunnel carries only new requests, and the TLS argument in Q1 excludes resending old bytes.
- **Application restart:**
  - there is no persisted outbound request queue;
  - `startup_reconciliation.py` contains no write call;
  - the dark `protection_operations` table is not registered and has no readers or writers (D§2.3);
  - shutdown waits for in-flight writes up to `SHUTDOWN_INFLIGHT_WAIT_SECONDS` (`instance_lock.py:841-844`) and does not resend;
  - a write killed mid-flight has an unknown outcome and is never replayed.
- **Background loops:**
  - a write that failed is not resent blindly;
  - `trailing_engine` retries a `modify_gtt` on the next cycle only after recomputing the stop and re-reading the held quantity under the protection lock (`trailing_engine.py:802-830`). That is a new absolute-state `PUT`;
  - `position_quantity.sync_protection` re-reads the GTT book before every resize (`position_quantity.py:243, 290-327`).
- **Emergency Exit:** after a failed `cancel_order` it re-reads the fill and sells only broker-confirmed quantity. It never repeats the cancel (`kill_switch.py:69-78, 389-404, 448-458`).
- **Browser:** the browser → `127.0.0.1` hop could resend a POST on a reused socket [I]. The ticket claim (409) blocks a duplicate execute [T].

### Q8. Evidence available, and what stays unknown

**Verified:**

- every client-side layer, at the installed versions;
- the client TLS trust chain;
- the absence of retry configuration in `backend/`;
- the existing single-send tests.

**Documented, not reviewed:**

- Squid as a CONNECT-only proxy on `10.77.0.1:3128` with a WireGuard source ACL (`DEPLOYMENT.md:160-215`; `NET1_KITE_REST_PROXY_REPORT.md:44-58, 225-234`);
- the 2026-11-04 field evidence that a down proxy fails the token exchange with no direct fallback (`DMB_KITE_OPERATIONAL_RUNBOOK.md:87`).

**Unknown without the broker or the infrastructure:** listed in §9.

## 4. File and line reference index

| Area | References |
|---|---|
| NET-1 parse and mapping | `backend/kite_rest_proxy.py:62-65, 72-108` |
| Client factory, narrowing, write fence | `backend/kite_client.py:66-108, 118-140, 155-195, 198-213` |
| Write-fence context manager | `backend/instance_lock.py:649-663, 841-844` |
| Write call sites | `backend/execution.py:92, 394-408, 461-532, 578-591`; `gtt_cancellation.py:54-96`; `position_quantity.py:243, 290-327`; `trailing_engine.py:802-830`; `strategy_engine.py:670-694`; `routes/orders.py:133-135`; `routes/kill_switch.py:69-78, 389-404, 448-458` |
| kiteconnect 5.0.1 | `connect.py:38` (timeout 7), `202-217` (session, pool, `disable_warnings`), `336-391`, `680-767`, `863-873`, `875-945` |
| requests 2.34.2 | `adapters.py:81, 205-211, 307-345, 634-744`; `sessions.py:154-184, 186-307, 309-329, 370-392, 488, 492, 502-503, 752-826, 845-860`; `models.py:96-104` |
| urllib3 2.7.0 | `connectionpool.py:288-290, 456-540, 758-886, 891-927, 942-971, 1042-1054`; `util/retry.py:197-202, 389-450, 452-543` |
| CPython 3.13.14 | `http/client.py:846, 1008-1010, 1065` |
| Tests (cited, not re-run) | `tests/test_kite_rest_proxy.py:429-435, 456-477, 500-503`; `tests/test_proxy_mandatory.py:166-183` |

## 5. Dependency versions and retry configuration

| Component | Version (installed) | Retry | Redirects | Timeout |
|---|---|---|---|---|
| kiteconnect | 5.0.1 (`requirements.txt` pin) | none | `allow_redirects=True` hard-coded | 7 s, applied to connect and to read |
| requests | 2.34.2 (transitive; not pinned) | `Retry(0, read=False)` | follows 301/302/303/307/308, at most 30 | from the caller |
| urllib3 | 2.7.0 (transitive; not pinned) | `total=0`, `read=False`, `status_forcelist=None` | disabled (`redirect=False`) | from requests |
| certifi | 2026.7.22, `cacert.pem` matches `RECORD` | — | — | — |
| Python | 3.13.14 (venv, `include-system-site-packages = false`) | `http.client`: none | — | — |
| Squid / WireGuard | not in repository | **[U]** | n/a for CONNECT | **[U]** |

requests and urllib3 are not pinned. A future upgrade could change these defaults without any visible diff in this repository.

## 6. Failure-mode analysis

| # | Mode | Automatic replay? | Classification at this HEAD |
|---|---|---|---|
| F1 | Connect, proxy or TLS failure before the request | No | Definite non-delivery; currently recorded as UNKNOWN, which is the conservative choice |
| F2 | Reset during or after transmission; read timeout | No | Ambiguous: UNKNOWN for orders and GTT cancels |
| F3 | Accepted at Kite, response lost | No | Ambiguous: UNKNOWN; the order tag allows discovery |
| F4 | 5xx with JSON `NetworkException` / `GeneralException`; or non-JSON | No | Ambiguous: UNKNOWN (not in `DEFINITE_REJECTIONS`) |
| F5 | **307/308 from Kite on a write** | **Yes, by requests** | Silent resend with body and token; the application never sees the 3xx |
| F6 | 301 on `PUT`/`DELETE`; 302/303 on a write | Partly (body-less resend or `GET`) | Server-directed; the result is attributed to the original call |
| F7 | Squid with `ssl_bump` | n/a | Fails closed at certificate verification |
| F8 | Restart of proxy or app mid-flight | No | Ambiguous outcome, never replayed |
| F9 | Ambiguous protective `place_gtt` (frozen `execution.py`) | No | Recorded as `protection_failed` / unprotected, although a GTT may exist. Not a replay, but a manual or automated "re-place" can duplicate it. A Phase 3 design input. |
| F10 | `OrderException` carried by a 5xx response | No | Treated as a definite rejection because the SDK maps by `error_type`, not status. Whether Kite ever returns `OrderException` for a request it partly processed is [U]. |

## 7. Classification: **CONDITIONALLY SAFE**

**Verified safe:**

- no layer in the client stack resends a request after a connection failure, timeout, reset or 5xx;
- no restart replays an in-flight request.

**Safe only under conditions that are not verified:**

1. **C1. Kite never answers a write with 307, 308, or 301 for `PUT`/`DELETE`.** If it does, requests resends automatically, through code that is verified present (`connect.py:911`, `sessions.py:250-258`). Kite does not document such redirects; that is not confirmation. **Removable on the client side, by M1.**
2. **C2. Squid is a CONNECT-only tunnel**, as documented. If it is not, TLS verification fails closed and nothing is replayed. C2 therefore affects availability, not replay safety; it is listed for completeness.

The verdict is not SAFE because C1 depends on broker behaviour that nobody has confirmed, and the redirect code is verified to be there. It is not UNSAFE because no layer resends after an ambiguous failure, and the only resend path needs a server-issued redirect from the authenticated endpoint.

## 8. Recommended mitigation (smallest first; none implemented)

- **M1. No redirect is ever followed for a broker write.** Either option is about one line in `kite_client.create_kite_connect`, next to `trust_env = False`, and does not touch `execution.py` or any frozen file:
  - **Option (a):** set `kite.reqsession.max_redirects = 0`.
    - `resolve_redirects` then raises `TooManyRedirects` on the first 3xx, before anything is resent (`sessions.py:216-219`).
    - That exception is a `RequestException`. The order paths already record it as UNKNOWN, which is correct, because the server answered and its outcome is not proven.
    - It also stops reads from following redirects; Kite reads do not need them [I].
  - **Option (b):** keep reads unchanged with a session response hook that raises on any 3xx response to a non-GET request. Hooks run before `resolve_redirects` (`sessions.py:791, 802`).
- **M2. Pin the transport policy with tests**, all offline:
  - assert that the factory's client has `max_retries.total == 0` and `read is False` on its adapters;
  - assert that no adapter other than the default is mounted;
  - assert `max_redirects == 0`, or that the hook is present;
  - add a real-stack test in which a local TLS-free mock answers a `POST` with a 307 and only one request arrives;
  - optionally pin `requests` and `urllib3` in `requirements.txt` so an upgrade cannot change these defaults silently.
- **M3. One rule for every write: an ambiguous outcome is UNKNOWN and is never retried automatically.**
  - Every broker write whose outcome is not a definite Kite refusal is recorded as UNKNOWN.
  - Only a broker-state read-back resolves it: the order book by tag, or the GTT book by content.
  - No automatic resend happens before that read-back proves the first attempt absent.
  - This already holds for orders and GTT cancels. Phase 3's journal must apply it to GTT create, which has no tag (F9), consistent with D§4.2 (`kind='create' ⇒ attempts ≤ 1`).
- **M4. Optional: separate read and write transport policies.** This is not needed if M1 uses option (a). The NET-1 report's argument for one shared client still holds; M1 changes only redirect behaviour on that client.

## 9. Remaining questions for the broker or the infrastructure

1. **Squid** (VM owner): `squid -v`, plus confirmation that `ssl_bump` is absent, that `http_access` allows only `CONNECT` to `api.kite.trade:443` from `10.77.0.2`, and the values of `connect_retries`, `forward_max_tries` and `retry_on_error`. Expected to be irrelevant to replay (Q1), but it is unverified.
2. **Kite** (Zerodha support or forum):
   - Can any `api.kite.trade` write endpoint (`POST /orders/*`, `PUT`/`DELETE /orders/*`, `POST`/`PUT`/`DELETE /gtt/triggers*`) return a 3xx?
   - Does Kite's edge retry requests to the OMS on its own?
3. **Kite idempotency:**
   - Is there any idempotency key?
   - Is a repeated `tag` deduplicated? (Assume not.)
   - Can `place_gtt` carry a client reference? (The SDK sends none.)
4. **Kite 5xx semantics:**
   - After a `NetworkException` (503) or `GeneralException`, may the order or GTT have been created?
   - Can `OrderException` ever accompany partial processing?
5. **Kite repeated modify or delete:** is a resent identical `modify_gtt` or `delete_gtt` a no-op or an error? This matters for D§4.2's exposure-reducing retry class.

## 10. Phase 3 recommendation: **NO-GO at this HEAD, then GO once M1 lands**

**Reasons for NO-GO now:**

- Phase 2 §13 item 1 says replay "must be disabled before any journal-driven write".
- A verified automatic resend path exists (F5).
- The condition that keeps it inactive (C1) is broker behaviour that nobody has confirmed.

**What turns this into GO for U6:**

- M1 implemented, with the M2 tests.
- Neither needs broker access or a change to a frozen file.
- After that, every identified layer sends a write at most once. U6 is closed, and §9 items 1–5 become documentation and design inputs rather than blockers.

**Unchanged by this report:**

- The other Phase 3 prerequisites stand: the D§11.2 amendments and v12 renumbering, the pending WIN-INT items, and U1.
- `LIVE_ENTRIES_ENABLED` stays `False` (`backend/live_entry_gate.py:40`).
- Live trading stays NO-GO.

## 11. Integrity of this investigation

| Check | Result |
|---|---|
| Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged |
| Worktree | Before/after sha256 of every tracked and untracked non-ignored file (scratchpad `before.json` / `after.json`): the only addition is this report; nothing else changed |
| Protected files | `.env`, `backend/ui_prefs.json`, `backend/kite_terminal.db*`: size and mtime via `os.stat` only, unchanged; never opened |
| `backend/execution.py` | identical to HEAD |
| Network | No broker call, no external request, no Kite MCP tool. No tests were run. |
| Standing backup practice (git tag and branch, database copy) | Not done, deliberately: this task is read-only and forbids database access and git writes. |
