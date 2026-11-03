# DMB Research Inbox

**PRIVATE RESEARCH CONTEXT — NOT A TRADE RECOMMENDATION OR ORDER INSTRUCTION.**

The inbox shows Daily Market Byte (DMB) Radar candidates that the user chose to import, with
their evidence and provenance. It is a one-way, file-based research handoff:

```
DMB Private Desk ──(explicit export command)──> immutable JSON bundle on disk
      ──(user presses Import, logged in)──> Kite research inbox
      ──(user presses Start ticket)──> the existing New Ticket form (symbol, exchange, rationale)
      ──(user types every trading detail)──> the unchanged manual-ticket path (risk checks, P1-D review, approval)
```

There is no network connection between the two applications in either direction, no automatic
import, no automatic ticket, no BUY/SELL decision, no order, no feedback to DMB, and no change to
the strategy universe or any trading configuration.

The wire contract (packet schema `private-candidate-packet-1.2`, bundle `dmb-candidate-bundle-1`,
canonical JSON, packet and bundle ids) is owned by DMB: `daily_byte/docs/PRIVATE_DESK_KITE_INTEGRATION.md`.
This terminal re-implements the checks independently in `backend/dmb_inbox_contract.py`; the two
repositories share no code.

## Real-data status

**TECHNICAL INTEGRATION COMPLETE — REAL SOURCE-DATA HANDOFF BLOCKED PENDING RIGHTS CONFIRMATION.**

DMB marks every real source's *private handoff* permission UNRESOLVED, so its exporter refuses
to write a bundle from real evidence (`RIGHTS_BLOCKED`). This terminal independently refuses any
packet whose `rights` are not explicitly `PERMITTED` for every source. The only bundles that can
pass today are DMB's synthetic test fixtures; they are labelled **SYNTHETIC** everywhere and their
rationale starts with `[SYNTHETIC TEST PACKET]`. This is an operating decision, not a legal
conclusion.

## Integration profile: synthetic bundles only in the isolated test profile

Synthetic bundles are **refused in production**, at import (`SYNTHETIC_NOT_PERMITTED`) and for any ticket handoff. They are actionable only in the isolated synthetic test profile (`backend/integration_profile.py`):

- **How it is entered:** only a test harness can enter it: `tests/demo_server.py --dmb-synthetic-profile`, and the test suite. There is no environment variable, setting or route for it.
- **What it requires:** a throwaway database and FakeKite. Both are re-checked on every use.
- **What it forbids:** arming LIVE.

"Synthetic" is decided by the packet's content, not the `data_origin` label alone. The checks:

- **Any marker counts:** the synthetic source, a `synthetic…` artifact id, a `SYNTHETIC` rationale prefix or the synthetic regime reason.
- **Relabelling is refused:** a relabelled packet gets `SYNTHETIC_MISLABELLED` in every profile.
- **Fixture origin is required:** a synthetic packet must come from DMB's fixture (`synthetic_handoff_fixture:<id>`).

A packet claiming real DMB evidence is accepted only when every source is in `integration_profile.PRODUCTION_PERMITTED_SOURCES`. That list is **empty** until the owner confirms private-handoff rights.

The isolated acceptance test is `docs/DMB_KITE_ACCEPTANCE_TEST.md`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KITE_DMB_HANDOFF_DIR` | *(unset)* | Absolute path of DMB's handoff folder, e.g. `D:\Claude_WS\daily_byte\output\private_desk\handoff`. Unset = import off. The terminal only reads it. |

The folder itself, and each `<YYYY-MM-DD>` session folder inside it, must be real directories
(not a symlink or junction).

## Import

- **Requires an active Kite login and this launch's terminal credential** (SEC-1 P1-F). Every
  inbox write — import, dismiss, note, research watchlist, Start ticket, ticket link — is a
  normal write: Host, Origin, `X-Kite-Terminal` and the per-launch credential are checked by the
  global middleware before the route runs, and the route then requires the trading account and
  a fresh Kite session. Reads need the trading account only. No route builds a Kite client.
- The user picks one listed bundle; the request names a session date and a bundle id, both
  pattern-checked, never a path. The file read is `<dir>/<session>/<bundle_id>.json`.
- Path safety: a symlink, junction or other reparse point at the folder, the session folder or
  the file is refused; the resolved path must stay inside the folder; a file with more than one
  hard link is refused; the opened handle must be the file that was checked (device + inode);
  the open is read-only and bounded (2 MB). Nothing in the folder is written or deleted.
- The whole bundle is validated before anything is stored, then stored in one transaction. Any
  failure refuses the whole bundle and records a `REJECTED` import with its reason code.
- Importing the same packets again is `DUPLICATE` and adds nothing.

### What is refused (reason codes)

`UNSUPPORTED_BUNDLE_SCHEMA`, `UNSUPPORTED_PACKET_SCHEMA`, `UNKNOWN_KEY` (any depth),
`MISSING_KEY`, `FORBIDDEN_FIELD` (an order, account or credential key at any depth, after
normalising `positionSize` / `position-size` / `positionsize` to one key), `BUNDLE_TOO_LARGE`,
`TOO_DEEP`, `TEXT_TOO_LONG`, `LIST_TOO_LONG`, `INVALID_SYMBOL` (`^[A-Z0-9&-]{1,20}$`),
`UNSUPPORTED_EXCHANGE` (NSE only), `MISSING_PROVENANCE`, `MISSING_QUALITY`,
`RIGHTS_NOT_APPROVED`, `INVALID_TIMESTAMP` (naive, malformed, evidence before the session close
or after generation), `FUTURE_TIMESTAMP`, `STALE_EVIDENCE`, `INVALID_SESSION_DATE`,
`REVIEW_SESSION_MISMATCH`, `CALENDAR_UNCOVERED`, `CORRUPT_JSON` (including duplicate JSON keys
and NaN/Infinity), `DUPLICATE_PACKET_ID`, `CONFLICTING_PACKET`, `PACKET_ID_MISMATCH`,
`BUNDLE_ID_MISMATCH`, `MALFORMED`, `PATH_REFUSED`, `FILE_SUBSTITUTION`, `BUNDLE_NOT_FOUND`,
`HANDOFF_NOT_CONFIGURED`.

## Sessions and staleness (owner decision)

A packet remains actionable **until the next trading session completes**:

- `session_date` is the completed regular NSE session the evidence describes;
- `review_session_date` must equal this terminal's `trading_calendar.next_trading_day(session_date)`
  (a special session such as Sunday Muhurat trading is not a review session; a mismatch is
  refused, never re-interpreted);
- the packet expires at **15:30 IST on `review_session_date`**. A bundle already expired is
  refused at import; an imported packet that expires stays visible but can no longer start a
  ticket (`EXPIRED`);
- both years must be in the NSE holiday file. **2027 is not**, so a packet whose review session
  falls in 2027 is refused (`CALENDAR_UNCOVERED`) until NSE's 2027 circular is added. The 2027
  gate is not bypassed.

## Storage (migration v9 `dmb_research_inbox`)

| Table | Holds | Mutability |
|---|---|---|
| `dmb_inbox_imports` | every import attempt: requested ids, status, reason, file SHA-256 and size, bundle schema and time, counts, Kite session id | append-only (triggers) |
| `dmb_inbox_items` | every packet version verbatim, one row per `(account, packet_id)`, with `logical_key` = `DMB:NSE:<symbol>:<session_date>` | immutable except the supersession pointer, set once |
| `dmb_inbox_events` | `IMPORTED`, `SUPERSEDED`, `DISMISSED`, `NOTE_ADDED`, `WATCHLIST_ADDED/REMOVED`, `TICKET_STARTED`, `TICKET_LINKED` | append-only (triggers) |
| `dmb_inbox_ticket_links` | item → manual ticket created after Start ticket | `ticket_id` has **no** foreign key, so a link never constrains, cascades into or changes `trade_tickets` |

Revisions: one logical candidate is one DMB Radar candidate of one session (DMB's
`radar_candidate_history` primary key is `(session_date, instrument)`). The newest version
(by `evidence_as_of`, then `generated_at`, then import order) is current; every older one is
marked superseded by it, once. Nothing is deleted. Existing trading tables are untouched.

## Actionability (re-evaluated on every read and at Start ticket)

An item can start a ticket only if none of these hold: `SUPERSEDED`, `DISMISSED`,
`INTEGRITY_MISMATCH` (the stored packet no longer hashes to its id), `RIGHTS_NOT_APPROVED`,
`CALENDAR_UNCOVERED` / `INVALID_SESSION_DATE` / `REVIEW_SESSION_MISMATCH`, `EXPIRED`,
`QUALITY_FRESHNESS`, `QUALITY_RADAR_COVERAGE`, `QUALITY_REPLAY_NOT_MATCHED`,
`INSTRUMENT_UNRESOLVED` (the symbol is resolved against the **local** instrument cache only —
never a broker call). Degraded items stay visible for research, marked "research only".

## User actions

- **Dismiss** (once), **Add note**, **Add to / remove from research watchlist** — each an event.
  The research watchlist is the inbox's own list. It is **not** the Kite `watchlist` table, which
  is the strategy engine's scan universe; an inbox action never changes what the strategies scan.
- **Start ticket** re-checks actionability, records `TICKET_STARTED` with a one-time nonce and
  returns only `{symbol, exchange, rationale}`. The New Ticket form opens with the symbol and the
  rationale filled in and the exchange shown; Direction, Entry order type and Product start on an
  unselected placeholder the browser's `required` check refuses; quantity auto-fill is off; every
  price is empty. The evidence `direction` never reaches the form. The ticket is created by the
  unchanged `POST /api/tickets` (risk checks, P1-D review stamp), approved through the unchanged
  confirm path (review token, live arming, reconciliation and worker-health checks, session
  binding, trading lock), and only then linked to the item. A link is refused unless the ticket is
  a manual ticket for the same symbol and exchange, created after that Start ticket, carrying its
  nonce, and not already linked.

## Not done / limitations

- Real-data handoff is blocked (see above). No source permission was changed by this work.
- Symlink cases are tested with real symlinks only where the OS allows creating them. On this
  Windows machine (no symlink privilege: WinError 1314, re-checked 2026-11-03 without elevating)
  the 3 real-symlink tests are skipped and the refusal branch is exercised with a simulated
  `lstat` instead. Junctions and hard links are tested for real.
- An import reads the file once; a DMB-side bundle is immutable and read-only, so a later change
  on disk would be a different file and is refused by its ids.
