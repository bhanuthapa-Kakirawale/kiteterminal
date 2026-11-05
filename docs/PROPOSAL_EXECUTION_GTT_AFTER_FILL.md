# Proposal: create protective GTTs only for confirmed filled quantity

**Status: PROPOSED. NOT APPLIED. Needs separate authorisation.** `backend/execution.py` is hash-frozen by the PK-25 manifest (`research/pk25/validation_manifest.json`, `model_freeze.production_file_sha256`). This document describes the change; it does not make it.

Until this change, or an equivalent one, is authorised, implemented, tested and verified with the real broker, **`backend/live_entry_gate.LIVE_ENTRIES_ENABLED` stays `False`**, so every LIVE new entry is refused server-side.

## 1. The defect (frozen code, unchanged)

`execution._execute_live` (lines 443-550 at `bfc0f73`):

1. Sends the entry with `kite.place_order(...)`.
2. As soon as Kite returns an order id, which only means the order was **accepted**, it places a sell GTT for the **full ticket quantity**: OCO stop and target, or SINGLE stop when trailing.
3. If `place_gtt` raises, it returns `protection_failed` with status `executed`, and the entry stays live at the broker.

Why this is unsafe:

- **Acceptance is not a fill.** A LIMIT entry can rest all session, an AMO can wait overnight, and a MARKET order can stay pending or fill partly. For that whole time a sell GTT for the full quantity is live.
- **A GTT is not scoped to a position.** It is a sell order on the instrument, and on trigger it acts on the account's demat holdings. Sell GTTs on equity holdings are authorised through CDSL TPIN or DDPI (Zerodha support). If it triggers before the entry fills, or covers more than the entry bought, it can sell shares the account already held: long-term holdings, pledged shares, or another position.
- **Unverified with the real broker** (no live test was run, by rule):
  - whether Kite accepts a CNC sell GTT before the holding exists;
  - whether a triggered sell leg for more than the held quantity is rejected, partly executed, or sells other holdings.

  The safe assumption is the worst one.

## 2. What already mitigates it (implemented in unfrozen code, tested with FakeKite)

None of these is the fix.

| Layer | Where | Effect |
|---|---|---|
| Hard gate | `backend/live_entry_gate.py` (`LIVE_ENTRIES_ENABLED = False`), enforced in `account_safety.precheck`, `account_safety.final_gate` and `routes/tickets._refuse_disabled_live_entry`, which sits next to the only `execution.execute_ticket` call. Live Readiness `LIVE_ENTRY_GATE` is a blocker. | No LIVE entry can be sent or armed. Dry run, exits and Emergency Exit are unaffected. |
| MARKET only (interim) | `live_entry_gate.check_market_regular_only` | LIVE LIMIT and AMO entries are refused. Secondary only: a MARKET order can still be pending or fill partly. |
| No pre-existing sellable quantity (interim) | `live_entry_gate.check_no_presellable_quantity`, final gate | Refuses a LIVE entry if anything of the instrument exists in the account: holdings (`quantity`, `t1_quantity`, `collateral_quantity`, …, on any exchange or a matching ISIN), positions, open orders, active GTTs, or other live app tickets. Fails closed on unavailable or inconsistent data. |
| Honest states | `lifecycle._protection`, `gtt_observations` | `NOT_PROTECTED_PENDING_FILL` and `QUANTITY_EXCEEDS_POSITION`. PROTECTED only when an active GTT covers exactly the confirmed filled open quantity. The GTT's status and quantity are shown next to the confirmed fill. |
| Alerts | `order_monitor._resolve_entry` (every COMPLETE fill verifies the GTT at the broker); `reconciliation._check_protection_missing` and `_check_gtt_staged_before_fill` | Urgent `PROTECTION_MISSING` for a filled or working entry with no GTT. Warning `GTT_STAGED_BEFORE_FILL` for a GTT live before the fill. |

## 3. Proposed change

### 3.1 `execution._execute_live`: entry only

Send the entry order exactly as today, with the same tag, the same `DEFINITE_REJECTIONS` handling and the same UNKNOWN result. **Do not place a GTT.** Return `executed` with `entry_fill_status='pending'`, `kite_gtt_id=None`, and a new event type `protection_pending`. The message says plainly that the entry is accepted, not filled, and not yet protected.

### 3.2 Protection created by the order monitor, for confirmed filled quantity only

When `order_monitor` first observes `filled_quantity > 0` (partial or COMPLETE) for a live entry with `use_gtt=1` and no GTT:

1. **Claim** the creation in the database before the broker call: compare-and-set `protection_state: pending → creating` plus a creation timestamp. Only one writer can create, so a restart or a second loop can never place two GTTs.
2. **Place** the GTT for exactly `position_quantity.open_quantity(...)`, the broker-confirmed fill minus anything already sold, with the ticket's stop and target. Use the existing tick rounding and `buffered_stop_price`.
3. **Outcomes:**

   | Result | Action |
   |---|---|
   | Definite success | Store `kite_gtt_id`, set `protection_state=active`. |
   | Definite rejection (`OrderException` / `InputException` / `PermissionException`) | `protection_state=failed`, URGENT alert and `PROTECTION_MISSING` finding. **Never retried automatically.** |
   | Ambiguous (timeout, 5xx) | `protection_state=unknown`. The next cycle searches `get_gtts()` for an active GTT matching symbol, side, quantity and trigger values, created after the claim. Exactly one match: adopt it. None after a bounded window: `failed`, URGENT. More than one: `needs_review`, critical. **Never blindly re-placed**, because a GTT carries no tag. |

4. **Later fills** resize the GTT to the new open quantity (the existing `position_quantity.sync_protection`, unchanged).
5. **Entry ended:**
   - cancelled or rejected with no fill: no GTT ever existed;
   - partial fill then cancelled: the GTT covers exactly the partial fill (sync_protection).

### 3.3 Exits and Emergency Exit (unchanged in intent)

An exit never creates a GTT. A full exit fill removes the position's GTT (`remove_protective_gtt`); a partial exit resizes it to what remains. Emergency Exit cancels an unfilled entry rather than selling, sells only the confirmed open quantity, and creates nothing.

### 3.4 Modification

The app has no entry-modification path (no `modify_order`). If one is ever added, a modified quantity must re-run 3.2's sizing under the position's protection lock.

### 3.5 The window this leaves

Between the fill and the monitor's next cycle (`ORDER_MONITOR_INTERVAL`), a filled position is **unprotected, and says so** (`protection_state=pending`, lifecycle `PROTECTION_PENDING`). If the terminal is not running, the window lasts until it is. This is the deliberate trade: an honest, bounded protection gap is preferable to a sell order that can act on shares the entry never bought. The runbook must state it, and the first live trades should be supervised.

## 4. What the change touches

- `backend/execution.py` (frozen): remove the GTT block from `_execute_live` and add the `protection_pending` result. Supersede the PK-25 manifest entry, recording a new hash with the reason.
- `backend/order_monitor.py`: the creation step in 3.2.
- `backend/db.py`: a migration adding `trade_tickets.protection_state` and `protection_claimed_at`, plus the `protection_pending` event type.
- `backend/lifecycle.py`: the `PROTECTION_PENDING` state.
- `backend/live_entry_gate.py`: `LIVE_ENTRIES_ENABLED = True`, only after all of the above is verified.
- Tests (FakeKite): claim exclusivity, ambiguous `place_gtt` adoption (0, 1 or 2 matches), definite rejection, resize on later fills, restart mid-creation, Emergency Exit during `pending`, and the frozen-hash supersede.

## 5. Before LIVE can be considered again

1. Authorise and implement section 3, then run the full suite.
2. Read-only checks against the real account. These are not orders: profile, holdings, margins, `order_margins` and the GTT book, through the GCP proxy.
3. A supervised first trade per `docs/FIRST_LIVE_TRADE.md`: one share, regular MARKET, with no pre-existing holding of the instrument. Watch the fill, the GTT's creation and its quantity in Kite.
4. Keep the interim guards, or remove each one with a documented reason.
