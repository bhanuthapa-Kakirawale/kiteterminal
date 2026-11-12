# Phase 9: Kite broker-validation matrix (preparation only)

**Date:** 2026-11-11 · **Status: PREPARATION. No broker contact, no credentials, no Kite MCP tool and no network
call were made.** Every "FakeKite model" below is a model written for the tests; a passing FakeKite test says nothing
about Kite. Register IDs (B-nn) are R6 §5 (`docs/PHASE_3_DESIGN_DECISIONS.md`).

**Safe validation methods used in the "method" column**

- **DOC**: Kite Connect documentation / Zerodha support answer, recorded with date and link.
- **RO**: read-only API calls (`get_gtts`, `orders`, `order_history`, `holdings`, `positions`) against the user's
  account, by the user or under separate authorisation, with no write.
- **S1**: a supervised one-share experiment on a low-priced liquid NSE stock, during market hours, by the user, with
  the engine NOT wired, each step logged (request, response, a read before and after), and a written abort plan.
- **NONE**: cannot be validated safely; the engine must stay fail-closed permanently.

"Can operate without the evidence?" means: can the application run **live** safely while the item is unverified,
given the engine's fail-closed posture. "No" items are live-money blockers.

| # | Behaviour (register) | Approved R6 requirement | Current FakeKite model | Actual Kite behaviour | Safe validation method | Evidence required | Risk if unverified | Operate without it? |
|---|---|---|---|---|---|---|---|---|
| 1 | DDPI/TPIN for a GTT-triggered CNC SELL (B-05, U1) | "Triggered, order rejected, position open" → `PROTECTION_FAILED`/`NEEDS_REVIEW`; never protected | `trigger_gtt(ddpi_rejected=True)` → REJECTED order (illustrative text) | **Unknown** for this account | DOC + RO (account DDPI status in Console) + S1 (one-share GTT trigger) | DDPI/POA status; one triggered GTT's order outcome | A stop that fires but cannot sell: the position is unprotected while shown as protected | **No** (LIVE-BLOCKER) |
| 2 | GTT creation semantics: placement right after a fill, before holdings show the shares (B-02) | Placement refusal = `rejected`; position stays `PROTECTION_PENDING`; a new create needs a new op | `place_gtt_refuse_until_holding_visible` | Unknown | S1: buy 1 share, place the GTT at once, then again after holdings update | Refusal class and message, or acceptance; timing | Repeated unprotected interval after every entry | **No** (LIVE-BLOCKER) |
| 3 | Duplicate GTT creation: does Kite deduplicate identical GTTs? one request → two GTTs at the edge? (B-01, B-24, B-25) | Assume no dedup; two fingerprint matches → `needs_review`; an id-less create makes S_agg UNKNOWN (R5-1) | no dedup; `place_gtt_creates_duplicate` | Unknown | DOC/support only (deliberate duplicates on a real account are not safe to provoke) | Written statement on idempotency / edge retries | None for safety (fail-closed); availability (more `needs_review`) | Yes (fail-closed) |
| 4 | GTT modification semantics: keeps the id, atomic, refused after trigger (B-07) | Read back the target id; missing → `needs_review` (row 15b) | modify keeps the id; refused unless active | Unknown | DOC + S1 (modify a one-share GTT's trigger, read back) | id unchanged; legs/trigger after; error after trigger | A modify that silently replaces the id would orphan protection | Yes (fail-closed: missing → review) |
| 5 | Repeated modification: no-op or error (B-27, G5) | Re-send only after a read shows the pre-state (M-3), so either semantics is safe | `repeat_modify_semantics = noop/error` | Unknown | S1 (same modify twice) | Response class of the second call | None for safety | Yes |
| 6 | GTT cancellation semantics: delete is final; status after delete (B-17, B-32) | Only an explicit `deleted`/`cancelled` confirms; acknowledgement never does | delete → status `deleted` | Unknown | S1 (delete a one-share GTT; read back several times) | Status string and how long it stays listed | Over-cautious refusals (EE) if the status differs | Yes (fail-closed) |
| 7 | Repeated cancellation: no-op or error; error class for an already deleted / triggered GTT (B-17, B-27, B-30) | Cancel retries only after a read shows `active`; "does not exist" + ACTIVE listing → URGENT | `repeat_delete_semantics = noop/error`; `InputException` for unknown ids | Unknown | S1 (delete twice; delete a triggered one in a later experiment) | Exception class and message | None for safety (retry is read-gated) | Yes |
| 8 | Triggered-order visibility: a triggered GTT's order link and when the order appears (B-13) | T(m) incomplete while an expected order is not visible; member stays in 𝒢 (R6) | `trigger_gtt` sets `result.order_result.order_id`; `hide_triggered_order_calls` | Unknown | RO after a natural trigger, or S1 with a GTT set to trigger immediately | `get_gtts` result block and `orders()` timing | EE refuses longer (fail-closed); never a wrong sale | Yes (fail-closed) |
| 9 | Partial fills of a triggered LIMIT leg (B-18) | r(o) = quantity − filled; an unfilled remainder counts as cancelled only when the broker reports CANCELLED/REJECTED | `trigger_gtt(status='OPEN', filled_quantity=k)` | Market behaviour | RO of a real partial (cannot be provoked safely) | `filled_quantity`, `pending_quantity`, `cancelled_quantity`, status vocabulary | Disclosed unprotected interval in a gap | Yes (disclosed residual) |
| 10 | Multi-leg (two-leg OCO) GTT behaviour and OCO exclusivity | Never assumed: every SELL leg is summed; one terminal leg never removes a two-leg GTT (R4, R5-2) | two-leg GTT; legs trigger independently | **Unknown** | DOC + S1 (two-leg GTT, trigger one leg) | Whether the other leg is cancelled, when, and its listed status | Over-counting only (EE refuses longer) | Yes (fail-closed) |
| 11 | GTT listing completeness and pagination (B-08, U8) | Membership never depends on the listing alone; settle counts complete reads; missing ≠ gone | `gtt_listing_limit` (single page), `gtt_listing_omit_ids`, listing lag | **Unknown** (no documented paging) | DOC + RO (count GTTs vs Kite web) | Whether `get_gtts` returns every GTT; any limit | A GTT absent from every complete read cannot be counted (disclosed B-08 residual) | **No** (LIVE-BLOCKER; sets DD-8) |
| 12 | Deleted / pruned GTT visibility and retention (B-10, B-28) | Missing never confirms a cancel (row 15a, G5/G9/G12); 30-day watch | `deleted_gtt_listing = "pruned"` | Unknown | RO repeated after a delete (S1 #6) | How long deleted GTTs stay listed, with which status | Cancels end in `needs_review` (availability) | Yes (fail-closed); evidence may relax row 15a |
| 13 | Delayed broker processing after a client timeout (B-31, U6M§8) | In-flight grace = SDK timeout + 5 s; ambiguous = `unknown`, never re-sent; late responses by M-4 | `accept_then_raise`, `accept_then_hold`, `on_call` delays | Unknown | DOC/support (cannot be provoked safely) | Maximum processing delay after the 7 s timeout | Sets the grace; too short a grace shortens the EE wait | Yes (fail-closed), but the grace value needs it |
| 14 | Session expiry: effect on GTTs; error class on writes (B-15, B-30) | GTTs assumed to persist server-side; Token on delete = definite, on create = ambiguous | `expire_session` | Unknown | DOC + RO after a natural expiry | GTTs still listed after re-login; error classes | Misclassified Token refusal on create would be ambiguous (safe) | Yes |
| 15 | Broker response ambiguity: 5xx, `OrderException` with partial processing, rate limits (B-16, B-26) | Anything not a definite class → `unknown` | `raise_on_next`, `accept_then_raise`, `fail_every_call` | Unknown | DOC/support | Exception taxonomy; whether 5xx can follow a processed write | None for safety (ambiguous = unknown) | Yes |
| 16 | Day rollover and CNC holdings visibility; T1 shares; holdings sellable by GTT (B-03, B-06, B-14) | Day-0 GTT shown "protective value unverified"; attribution `unknown` during visibility lag | `rollover(days)`, T1 fields, stale snapshots | Unknown | DOC + RO across a rollover (positions → holdings, T1) | When shares move positions → holdings; whether a GTT sells T1 | A stop that cannot sell T1 shares on day 1 | **No** (B-03 LIVE-BLOCKER) |
| 17 | Over-quantity triggered GTT: rejected, partial or short sale (B-04) | Never relied on: EE needs G_max = 0 and S_agg = 0; `caps_sell_exposure` exists because of it | policy `reject` | Unknown — **never tested live** | DOC only (provoking it risks a short delivery sale) | Written statement | Underpins the cap and any future EE relaxation | **No** (LIVE-BLOCKER) |
| 18 | Proxy replay / duplicate delivery (B-22, U6) | Client-side resend closed (`max_redirects=0`); proxy replay unverified | n/a | Unknown | Config review of the Squid VM by its owner | `forward_max_tries`, `retry_on_error`, no `ssl_bump` | A duplicated write the engine would see as ambiguous | **No** (LIVE-BLOCKER) |
| 19 | GTT id reuse (B-29) | Different ids never merged; ids are keys | ids monotonic | Unknown | DOC/support | Statement that ids are never reused | A reused id could alias two GTTs | Yes (fail-closed) |
| 20 | GTT status vocabulary (B-32) and order status vocabulary | Only `active` armed; only `deleted`/`cancelled` confirm; unknown order status → UNKNOWN | the statuses FakeKite uses | Unknown | DOC + RO | Full status lists | Unknown status → review / refusal (availability) | Yes (fail-closed) |

## Residuals that prevent live-money use (from this matrix)

1. DDPI/TPIN for unattended GTT sells (row 1).
2. GTT placement right after a fill (row 2).
3. Holdings availability at trigger time; T1 (row 16).
4. Over-quantity triggered GTT behaviour (row 17).
5. GTT listing completeness / pagination; the real DD-8 values (row 11, and rows 12–13 for the parameters).
6. Proxy replay configuration (row 18).

Plus the engineering blockers in the final report (§J): legacy-path fencing (B-L1/B-L2), the M-6 (c) legacy resume
detection, Phase 4 (`execution.py`), Phase 6 wiring of Emergency Exit, v12 registration with the operator backup.

## Procedure outline for the supervised experiments (S1), for later authorisation

1. Preconditions: separate written authorisation; engine not wired; `LIVE_ENTRIES_ENABLED = False` (entries placed
   manually in Kite, not by the app); one share of a liquid stock; market hours; a fresh operator DB backup.
2. Before each write: a read-only `get_gtts`/`orders`/`holdings`/`positions` snapshot, saved.
3. The write, by the user in Kite or through a one-off authorised script with the full request/response logged.
4. After each write: reads at +0 s, +5 s, +30 s, +2 min, +10 min; saved.
5. Abort plan: if any response is unexpected, stop, delete the GTT in Kite, and record the state.
6. Every result goes into this matrix with its date, and maps to a configuration value or a design change; no
   relaxation of a fail-closed path without a new written decision.
