# Audit remediation: S2/S3, G7, E3, M3, C4, C8

Controlled local implementation of six findings from the independent read-only audit.
Starting point: branch `feature/signal-detail-and-strategy-revision`, HEAD `35a0e07`, clean tree.
Nothing committed. FakeKite and scratch databases only; no broker call, no Kite MCP tool, no
broker write enabled. `LIVE_ENTRIES_ENABLED` stays `False`; `backend/execution.py` and every
other PK-25 hashed file are byte-identical. The GTT-after-fill proposal
(`docs/PROPOSAL_EXECUTION_GTT_AFTER_FILL.md`) is **not** implemented.

## S2/S3: GTT protection truthfulness

**Was:** with no broker observation, `lifecycle.gtt_view` fell back to the app's own record and
`_protection` could return PROTECTED from database state alone. Observations had no age limit and
no instrument check. An accepted `modify_gtt` was recorded as an observation. A GTT that
disappeared from the book kept its last "active" sighting.

**Now:**
- `backend/gtt_observations.py` stores only what a broker read reported: status, the condition's
  exchange, symbol and token, every leg (side, quantity, exchange, symbol) and `observed_at`.
  `record_book(..., expected=[ids])` records an expected id that is absent from a complete read as
  `missing`. `invalidate()` replaces `record_resize`: an accepted modify is not an observation.
- Every existing GTT read now records the book: reconciliation, the GTT watcher, the order
  monitor's post-fill check, `sync_protection`, `remove_protective_gtt`, the GTT page, the
  presellable guard and the cancellation read-back. A successful resize is followed by one
  read-back GET; if that read fails, the GTT stays unobserved.
- `config.GTT_OBSERVATION_MAX_AGE_SECONDS = 2 × RECONCILIATION_INTERVAL_SECONDS + 60` (420 s).
- `lifecycle._protection` returns a status and a reason:

  | Status | Meaning |
  |---|---|
  | `PROTECTED` | A fresh broker observation of an active GTT on this instrument, with every leg a SELL of exactly the confirmed held quantity |
  | `NOT_PROTECTED` | Protection is missing or incorrect for a filled open position. Reasons: `NO_GTT`, `GTT_NOT_ACTIVE`, `INSTRUMENT_MISMATCH`, `LEGS_INVALID`, `QUANTITY_EXCEEDS_POSITION`, `QUANTITY_BELOW_POSITION`, `UNRESOLVED_QUANTITY_FINDING`, `PRICE_MISMATCH` |
  | `UNVERIFIED` | Never observed (restart, or no read yet) |
  | `STALE` | The observation is older than the maximum age. A terminal status (triggered, deleted and so on) is `NOT_PROTECTED` however old |
  | `PENDING_FILL` | An accepted, working entry with nothing filled. It is not an open position, so it is never counted as unprotected. The reason is `GTT_BEFORE_FILL` or `NO_GTT` |
  | `UNKNOWN`, `NOT_REQUESTED`, `NOT_APPLICABLE` | Unchanged |

  Database state can only lower the verdict. `gtt_status` and `gtt_quantity` are broker-observed
  only; the app's record is `gtt_recorded_quantity`. A paper GTT is observed from the paper
  broker's own `paper_gtts` book, which is that broker's state of record (one batched query per
  context).
- The summary's `unprotected` counts `NOT_PROTECTED`; a new `unverified` counts `UNVERIFIED` and `STALE`.
  The frontend shows the status, the reason, the observation age, and "not yet observed (app
  record …)".

## G7: fail-closed trading mode

**Was:** `paper_broker.broker_for` returned the real Kite client for any mode other than
`"dry_run"`, and `check_mode` accepted it. The live gates (`precheck`, `final_gate`, the LIVE-entry
gate) key on `mode == "live"`. So a NULL or malformed mode skipped every live gate and still
reached the real broker.

**Now:** `paper_broker.VALID_MODES = ("dry_run", "live")`, `InvalidTradingMode` (a
`ModeIsolationError`) and `require_valid_mode()` require `type(mode) is str` and an exact match:
no case-folding, no stripping, no default.
- `check_mode` and `broker_for` refuse anything else before any call. The frozen
  `execution.execute_ticket` already calls `check_mode` and turns `ModeIsolationError` into
  "failed, nothing sent", so it is covered without an edit.
- The order monitor, GTT watcher, protection upkeep, `remove_protective_gtt` and Emergency Exit's
  entry cancel all pass through `check_mode`. `sync_protection` does nothing for a non-tracked mode.
- `routes/tickets._claim_mode` refuses an invalid global or inherited mode with a 409
  `INVALID_EXECUTION_MODE` before the claim. A `broker_for` refusal after the claim is recorded as
  a definite failure, never UNKNOWN.
- Bypass review: no environment variable is read in the mode path (AST test). The API models are
  `Literal["dry_run", "live"]` (422 tests). The database CHECK constraints are backed by this
  check: tests write invalid modes with `PRAGMA ignore_check_constraints`.
- Not mode-routed, by design: the manual `POST /api/orders/{id}/cancel` and
  `POST /api/gtts/{id}/cancel` act on an id the user chose (a paper id goes to the paper broker);
  they are user actions on the broker's own books, not trading-mode routing.

## E3: Emergency Exit reconciliation

**Was:** each exit was sized to the app's `open_quantity`, with no comparison against the
account.

**Now (`routes/kill_switch.py`):** one fresh snapshot per click (holdings, positions and today's
orders) plus the app's own order ids and tags. That includes orders matched to recorded GTT
closures (`trailing_engine._app_order_ids`). Before each sell, per instrument
(`exchange` + `tradingsymbol`, and the token where the broker gives one):
- **Refused, `BROKER_RECONCILIATION_UNAVAILABLE`:** any read or parse failure. An expired session
  is called out.
- **Refused, `EXIT_RECONCILIATION_FAILED`:**
  - the instrument's broker rows are inconsistent;
  - the instrument token conflicts;
  - another open app position in the instrument has an unknown quantity;
  - the broker's total (settled + T1 + the day's CNC net, from `backend/broker_holdings.py`) is
    below the app's total open quantity across every open live position in the instrument;
  - a SELL of the instrument not placed by this app is working or has filled today (a manual
    sale, or a protective GTT that fired and is not yet recorded: a double-sell risk).
- Every refusal says to exit manually in Kite after checking holdings, positions and orders.
  Nothing is sent for that instrument, and other instruments are still attempted.
- **Allowed, with a note:** more shares held than the app's (the user's own; exactly the app's
  quantity is sold), and external BUYs today.
- Unchanged: authentication (`require_trading_account`), the phrase check, session handling,
  in-flight and duplicate-click detection (`already_exiting`, `exit_outcome_unknown`),
  per-position isolation, and the preview (database-only).

## M3: margin preflight

**Was:** MARKET orders sent `price=0` to `order_margins`; a missing `charges` defaulted to 0;
`float(entry_price)` ran outside the fail-closed block.

**Now (`backend/margin_preflight.py`):**
- **LIMIT:** sized at the limit price, which must be finite and positive.
- **MARKET:** sized at the upper circuit limit from a fresh `kite.quote()`. `last_price` and
  `upper_circuit_limit` must be finite and positive, with the circuit ≥ last price. Kite is asked
  about the equivalent LIMIT BUY at that price. `price=0` is never sent.
- `charges.total` is required and must be > 0; a delivery BUY always carries STT and stamp duty.
- Quantity, price and quote validation are all inside the fail-closed block.
- Kept as before: only cash-equity CNC BUY is supported (derivatives, MIS, NRML, NFO and SELL are
  refused). With writes disabled or no proxy, `order_margins` (a POST) is refused locally and the
  gate fails closed. No write switch was changed.
- **Remaining assumptions** (documented in the module, not verified against a real account):
  - Kite's field semantics;
  - the quote's upper circuit (or dynamic band) is the highest price a MARKET BUY can fill at;
  - a LIMIT CNC BUY's margin equals that of a MARKET one of the same value;
  - Kite RMS remains the final control.
- Deliberately conservative: funds between the expected value and the worst-case value are
  refused.

## C4: GTT cancellation outcomes

**New `backend/gtt_cancellation.py`:** one `delete_gtt`, then always a read-back `get_gtts`
(recorded as an observation).

| Outcome | When |
|---|---|
| `CANCEL_CONFIRMED` | The read-back lists the GTT `deleted` or `cancelled` |
| `CANCEL_FAILED` | Still `active`, or a definite refusal (Order/Input/Permission/Token) with no read-back |
| `CANCEL_OUTCOME_UNKNOWN` | No read-back after an acknowledged or ambiguous delete, the GTT is absent from the book, or another status (e.g. `triggered`) |

- The three call sites are the order monitor (a cancelled or rejected entry),
  `remove_protective_gtt` (after the exit filled) and the manual cancel route.
- Only `CONFIRMED` clears `kite_gtt_id` or says "cancelled" or "removed". `FAILED` and `UNKNOWN`
  keep the id, write a persistent finding (`GTT_CANCEL_FAILED` or `GTT_CANCEL_OUTCOME_UNKNOWN`,
  both critical, suggested action `CANCEL IN KITE` or `CHECK KITE`) and a ticket event, and raise
  an urgent alert:
  - the monitor broadcasts urgently;
  - `remove_protective_gtt` returns `urgent=True`;
  - the route returns 502 or 409 (401 for an expired session).
- Nothing is retried. Rows already recorded as `ORPHANED_GTT_AFTER_EXIT` keep their label.

## C8: holdings reconciliation

**Was:** `BrokerSnapshot.load_held` keyed by symbol only, summed `quantity + t1_quantity` plus
every net position of any product (so a CNC row carried overnight was double-counted and MIS was
counted as held), and silently skipped malformed rows. Findings were de-duplicated on
`actual = "N shares"` without the instrument.

**Now:** `backend/broker_holdings.instrument_quantities()` keys by `(EXCHANGE, TRADINGSYMBOL)`:
- settled = `quantity`; T1 = `t1_quantity`;
- day = CNC net `quantity − overnight_quantity` (the overnight part is already T1 in holdings);
- MIS and NRML are listed but never counted; `used_quantity` is not used, because today's sells
  are in positions.

Per-key problems cover duplicates, non-numeric or negative values, token conflicts and a negative
total. A row that names no instrument makes the read unusable for the cycle (fail closed).
- Reconciliation compares per instrument. A shortfall is
  `actual = "<EXCH>:<SYM> <n> shares"`, which also fixes the cross-symbol dedupe collision, and
  notes a same-symbol holding listed under another exchange. A key with problems becomes the new
  `BROKER_QUANTITY_UNVERIFIABLE` (urgent, symbol-scoped).
- **Limit:** `reconciliation_log` has no exchange column, so a symbol-scoped finding attaches to
  every open position in that symbol on any exchange. That over-attaches, which is the
  conservative direction. ISINs are not bridged across exchanges.

## Residual limits (documented, not fixed here)

- Observations are process memory: after a restart every position is UNVERIFIED until the first
  read (usually within one GTT watcher or reconciliation cycle).
- Kite's order book covers today only. External activity on earlier days shows up only as a
  quantity difference, or not at all (a sale later replaced by a purchase).
- No behaviour here has been verified against a real Kite account.
- The root defect (the protective GTT is placed before the fill, in the frozen
  `backend/execution.py`) is unchanged. LIVE new entries stay disabled.

## GO / NO-GO

| Area | Status | Notes |
|---|---|---|
| LIVE new entries | **NO-GO** | `LIVE_ENTRIES_ENABLED = False`. Needs the separately authorised GTT-after-fill change in `execution.py`, which is not implemented here, plus real-account verification. |
| Real-account verification of these fixes | **NOT DONE** | No broker call was made. Kite field semantics are unverified. |
| Dry run (paper broker) | GO | Exact `"dry_run"` only. Paper protection is judged from the paper broker's own book. |
| Protection display (S2/S3) | GO | Never PROTECTED without a fresh, correct broker observation. |
| Trading-mode routing (G7) | GO | Fails closed at the broker boundary for any mode that is not exact. |
| Emergency Exit (E3) | GO, with refusals | Sells only what fresh broker data supports; otherwise refuses and points to Kite. |
| Margin preflight (M3) | GO as a fail-closed gate | Worst-case MARKET pricing; behind the LIVE-entry gate, so it never runs in production today. |
| GTT cancellation (C4) | GO | Confirmed / failed / unknown are distinct; findings are persistent. |
| Holdings reconciliation (C8) | GO | Exchange + symbol identity, no double counting. Findings are symbol-scoped. |

## Tests

New files: `tests/test_gtt_protection_truth.py`, `tests/test_trading_mode_fail_closed.py`,
`tests/test_emergency_exit_reconciliation.py`, `tests/test_gtt_cancellation.py`,
`tests/test_holdings_reconciliation.py`. Existing tests were updated where they encoded the old
statuses or seeded an app position with no matching broker holding; each is commented in place,
and no safety assertion was weakened. Results are in the final report of this change.
