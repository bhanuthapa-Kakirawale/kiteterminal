# OPUS55 PK-14 + PK-15 Implementation Report: sector mapping and the exposure basis (P1-4, P1-5)

| | |
|---|---|
| Packets | **PK-14** (audit **P1-4**), then **PK-15** (audit **P1-5**), in that order. PK-16 and later were not started. |
| Baseline before | **669 passed**, 0 failed, 0 xfailed, 0 xpassed, re-measured on the untouched code before the first edit. |
| Result after | **718 passed, 0 failed, 0 xfailed, 0 xpassed**, three consecutive runs. 718 = 669 + 22 (`tests/test_sector_exposure.py`) + 27 (`tests/test_exposure_basis.py`). |
| Backups taken first | Tag `backup-pre-pk14-pk15` and branch `backup/pre-pk14-pk15`, both at `b4ba6ad` (the tree was clean). `backend/kite_terminal.db.pre_pk14_pk15_bak` was taken with the SQLite online-backup API: `integrity_check` ok, 18 tickets, 51 watchlist rows, 1 sector row. |
| Live DB | Untouched: SHA-256 `1378f8b47cd7…` and mtime identical before and after. Tests use a per-test temporary DB and FakeKite; seeding was validated on a scratch copy. |

# Executive Summary

**Before these packets:**

- **The sector limit was effectively off (P1-4).**
  - `sector_mapping` was seeded only from holdings.
  - An unmapped symbol passed the sector check as "skipped".
  - The live DB had exactly one mapped symbol, which a user had entered by hand.
  - A breakout day could therefore stack TCS, INFY and HCLTECH far past the 20 % limit.
- **Exposure ignored real shares (P1-5).**
  - The risk engine read `holdings.quantity` only. Yesterday's buy, which sits in `t1_quantity`, counted as **zero**, so a day-2 signal received a fresh 10 % single-stock allowance.
  - Same-day buys made outside the app were invisible, because `positions()` was never read.
  - The numerator used cost while the denominator used market value.
  - The denominator counted pledged holdings twice: once at market value, and again as the collateral margin inside `equity.net`.

**Now:**

- **PK-14:**
  - Every watched, held, position and traded symbol is seeded from the one static source, `SECTOR_LOOKUP`, at startup, after login and on watchlist add. Seeding never overwrites a manual mapping.
  - The risk engine resolves any symbol through the table first, then the lookup, so a strategy candidate is covered even before it is seeded.
  - A symbol with **no sector fails closed** for a new entry: `SECTOR_MAPPING_REQUIRED`.
  - The lookup now covers all 51 live watchlist symbols plus traded AWL.
- **PK-15:**
  - One broker snapshot (`holdings()`, `positions()`, `margins()`, one call each) feeds every exposure check of an approval.
  - Shares are counted **once**: `max(broker settled + T1 + today's CNC change, the app's PK-13 open quantity)`.
  - On top of that, conservatively, everything that may still fill: in-flight and UNKNOWN entries, and the unfilled rest of a working order.
  - Held shares are valued at **market**. Shares not yet bought are valued at their planned entry price.
  - The portfolio value excludes the collateral margin.
  - Anything unusable fails closed with `PORTFOLIO_EXPOSURE_UNAVAILABLE`, and never blocks an exit.

**Validation:**

- 49 new tests (22 for PK-14, 27 for PK-15), plus 4 restated tests;
- 10/10 focused mutations caught;
- the targeted suite ×10 and the exposure subset ×20, all green;
- full suite ×3: 718 passed each run;
- the DB seeding run twice on a scratch copy of the latest real backup.

# PK-14 Sector Mapping

## Previous gap (traced before any edit)

| Question | Finding |
|---|---|
| Where `SECTOR_LOOKUP` comes from | A hand-maintained dict in `backend/sector_mapping.py`, about 70 NSE large-caps. Kite has no sector endpoint. |
| How `sector_mapping` is populated | `seed_from_holdings(kite)` only, at startup (`main.on_startup`) and after login (`routes/auth._after_login`); plus manual rows from Settings (`upsert_sector`). |
| Which symbols received a mapping | Only **held** symbols that are also in the lookup. The live DB had 1 row (ATHERENERG → "Consumer Durables", manual). |
| Holdings | Seeded, but only if the symbol is in the lookup. |
| Watchlist stocks | **Never seeded.** 35 of the 51 were in the lookup and 16 were not. |
| Risk engine with no mapping | `get_sector` read the table only. `None` → `passed=True, skipped=True`: "unknown sector, exposure check skipped". |
| Symbols counted in a sector | `symbols_in_sector(sector)` from the table only, so a held symbol that was unmapped or unseeded never counted. |
| Lookup defect | The key `"INDUS TOWERS"` (with a space) can never match the tradingsymbol `INDUSTOWER`. |

## Mapping source and taxonomy

`SECTOR_LOOKUP` remains the **one static source**: no new source, no scraping, no network.

- **Fixes:**
  - `INDUS TOWERS` becomes `INDUSTOWER`.
  - `TMPV` and `TMCV` (the Tata Motors demerger) are added as Auto. `TATAMOTORS` is kept.
- **Additions** cover every live watchlist symbol and the traded AWL, which were verified read-only from the live DB (`immutable=1`):

| Sector | Members (**added** in bold) |
|---|---|
| IT | TCS, INFY, WIPRO, HCLTECH, TECHM, LTIM, MPHASIS, COFORGE, **LTTS, FRACTAL** |
| Banking | HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, AXISBANK, INDUSINDBK, BANKBARODA, PNB, IDFCFIRSTB |
| Financial Services | BAJFINANCE, BAJAJFINSV, HDFCLIFE, SBILIFE, ICICIPRULI, ICICIGI, **RECLTD, SHRIRAMFIN** |
| FMCG | HINDUNILVR, ITC, NESTLEIND, BRITANNIA, DABUR, TATACONSUM, MARICO, GODREJCP, COLPAL, **AWL** |
| Pharma | SUNPHARMA, DRREDDY, CIPLA, DIVISLAB, LUPIN, AUROPHARMA, BIOCON, ALKEM, **TORNTPHARM** |
| Auto | MARUTI, TATAMOTORS, **TMPV, TMCV**, M&M, BAJAJ-AUTO, EICHERMOT, HEROMOTOCO, TVSMOTOR, ASHOKLEY, **HYUNDAI, ATHERENERG** |
| Energy (oil & gas, power, coal) | RELIANCE, ONGC, NTPC, POWERGRID, BPCL, IOC, COALINDIA, GAIL, ADANIGREEN, **ADANIPOWER** |
| Metals | TATASTEEL, JSWSTEEL, HINDALCO, VEDL, JINDALSTEL, **HINDZINC** |
| Cement | ULTRACEMCO, SHREECEM, AMBUJACEM, ACC, **GRASIM** |
| Telecom | BHARTIARTL, IDEA, **INDUSTOWER** (fixed) |
| Infra (capital goods, including defence) | LT, ADANIPORTS, ADANIENT, SIEMENS, **BEL, HAL, SOLARINDS** |
| Consumer Durables | TITAN, ASIANPAINT, HAVELLS, VOLTAS |
| **Consumer Services** (new) | **DMART, ETERNAL, INDIGO** |

**Taxonomy policy.** The groups are broad on purpose: a sector limit only protects if related names share one bucket. The existing labels are kept as they are ("IT", not "Information Technology"; `list_sectors` already prevents spelling forks).

- **Banking vs Financial Services.** Banking stays separate from NBFCs and insurers, as before. This mirrors the Nifty Bank / Nifty Financial Services split. It is kept, not merged, because the brief says to preserve the taxonomy unless it is clearly broken, and it is not. The limitation is recorded below.
- **Defence.** Defence manufacturers (BEL, HAL, SOLARINDS) go into Infra / capital goods rather than a new one-name "Defence" bucket.
- **Consumer Services.** The one new sector covers retail, food delivery and aviation. These are the NSE "Consumer Services / Services" names that fitted no existing bucket.

**ATHERENERG.** The live DB's manual mapping ("Consumer Durables") is **preserved**, because manual always wins. The lookup lists it as Auto (it is an electric two-wheeler maker), which applies only if the manual row is deleted. **Worth a look by the user.**

## Seeding behaviour

| Function | When | What |
|---|---|---|
| `seed_symbols(symbols)` | every path below | `INSERT OR IGNORE` of the lookup's sector for the listed symbols. Never overwrites a row; sorted, deterministic, idempotent. Returns the rows added (`total_changes`). |
| `seed_known_symbols()` | `main.on_startup`, right after `init_db()`, **outside** the Kite try-block (no session needed; a failure is logged, not fatal) | every watchlist symbol ∪ every `trade_tickets` symbol |
| `seed_from_holdings(kite)` | startup (with a session) and after login, unchanged call sites | holdings ∪ `positions()` net symbols. A `positions()` failure still seeds the holdings. |
| watchlist add (`routes/watchlist.add_watchlist_symbol`) | after the insert | the new symbol |
| `resolve_sectors(symbols)` | every risk check | stored row (seeded or manual) → else `SECTOR_LOOKUP` → else `None`. One table read. Covers strategy candidates and anything not yet seeded. |
| `find_unmapped_symbols` | Settings → Unmapped Symbols | held ∪ position ∪ **watchlist** symbols whose sector resolves to `None`: exactly the ones that would be blocked |

A manual mapping is preserved by every path (test 12). Deleting it in Settings falls back to the lookup; there is no "no sector on purpose" state.

## Unknown-sector policy: fail closed

For a position-opening ticket whose symbol resolves to no sector, the check returns:

- `passed=False`, `skipped=False`;
- `code="SECTOR_MAPPING_REQUIRED"`;
- the label "Sector exposure · UNMAPPED";
- the message "Sector: UNMAPPED — BLOCKED, a sector mapping is required for X before a new entry (Settings → Sector mappings) [SECTOR_MAPPING_REQUIRED]".

Where it applies:

- **At creation** the ticket becomes `risk_rejected`. Mapping the symbol lets a new or edited ticket through (test 7d).
- **At confirmation** (PK-4 revalidation) the response is 409 `RISK_REVALIDATION_FAILED`, with `failed_checks: ["max_sector_exposure_pct"]` and a new `failed_codes: ["SECTOR_MAPPING_REQUIRED"]`. The ticket becomes `risk_rejected`, and nothing is sent.

What it never touches:

- **Not blocked:** exits, Emergency Exit, strategy exits and alert-action sells. `run_checks` returns before any position-opening check for `execution.is_closing_sell` tickets (tests 8 and 8b).
- **Never runs the check:** protection (GTT sync, trailing) and reconciliation.

**Dry run.** The same rule applies (test 7c). The execution mode is only claimed at confirmation (PK-11), so creation-time checks cannot know it. A dry run is therefore a faithful rehearsal of what live would refuse. Dry-run tickets never count toward any exposure.

**Why fail closed, and not an "UNMAPPED" bucket.** A bucket would cap unknown names together, but it would let any single unknown name through up to the sector limit, and it would mix unrelated stocks. Failing closed costs one click in Settings per new name. It is also the brief's preference.

**Unmapped holdings** do not block a trade in another sector. They are named in the message ("not counted, no sector mapped: X"), and they appear in Settings → Unmapped Symbols.

## Exposure calculation

Sector exposure = Σ exposure of every symbol that resolves to the ticket's sector (the PK-15 basis, below) **+ the proposed `entry_price × quantity`**, ÷ portfolio value. It passes if `≤ limit` (the brief's worked example: 150,000 + 75,000 on 1,000,000 = 22.5 % > 20 % → **refused**, test 5).

- The candidate symbols are every symbol with exposure: broker-held, or an app open or in-flight position.
- An exact-limit float tolerance (`_PCT_EPSILON = 1e-9` percentage points) is applied to the single-stock and sector checks. Exactly 20 % passes, and one share more fails (test 6).

## UI behaviour

The UI changes are minimal (`frontend/app.js`, `frontend/index.html`).

- **Risk-check rows** (confirm dialog and Signal Detail share one renderer):
  - "Sector exposure · IT" with **current 15.0 % → 16.5 % (max 20 %)**;
  - "Single-stock exposure" in the same form;
  - an unmapped symbol reads "Sector exposure · UNMAPPED" with **"Sector: UNMAPPED — BLOCKED, mapping required"**, plus the existing Settings → Sector mappings link.
- **Older stored results** (skipped, with no `current_value`) still render as before.
- **Settings:**
  - the header says the mappings are seeded from holdings **and watchlist**;
  - Unmapped Symbols notes that "new entries in these are blocked until mapped";
  - the empty state reads "every held or watched symbol has a sector".
- The Quick Action concentration row now reads "Exposure (market value)".
- `node --check frontend/app.js` passes. No browser run was made.

## Tests (`tests/test_sector_exposure.py`, 22)

| Brief # | Test |
|---|---|
| 1 | `test_1_startup_seeds_watchlist_and_ticket_symbols_from_the_lookup` |
| 2 | `test_2_holdings_and_todays_positions_are_seeded` (an NFO future is ignored) |
| 3 | `test_3_adding_a_watchlist_symbol_seeds_its_sector` |
| 4 | `test_4_known_sector_below_the_limit_passes_and_shows_both_figures` |
| 5 | `test_5_worked_example_22_5_percent_is_refused` |
| 6 | `test_6_exactly_at_the_limit_passes_and_one_share_over_fails` |
| 7 | `test_7_…fails_closed_at_creation`, `test_7b_…at_confirmation` (409, `failed_codes`, nothing sent), `test_7c_…dry_run`, `test_7d_mapping_the_symbol_by_hand_unblocks_it` |
| 8 | `test_8_emergency_exit_of_an_unmapped_symbol_is_unaffected`, `test_8b_alert_action_sell_of_an_unmapped_holding_is_unaffected` |
| 9 | `test_9_several_same_sector_holdings_aggregate`, `test_9b_open_app_positions_in_the_sector_count` |
| 10 | `test_10_the_proposed_order_is_included` |
| 11 | `test_11_startup_seeding_is_idempotent_and_deterministic` |
| 12 | `test_12_a_manual_mapping_is_never_overwritten_by_any_seed` |
| extra | lookup fixes resolve; **every live watchlist symbol has a sector**; unmapped listing covers the watchlist; an unmapped holding is named but never blocks; dry-run tickets never count |

**PK-14 checkpoint.** Before PK-15 started, on the old exposure basis:

- the new file: 22 passed;
- `test_live_safety`, `test_baseline_api`, `test_alert_action`, `test_account_safety*`, `test_signal_detail`, `test_mode_binding` and `test_review_changes` passed, apart from one expected restatement (`test_sector_mappings`, below);
- `test_emergency_exit_isolation`, `test_pk9_pk11_cross` and `test_partial_fills`: 108 passed.

# PK-15 Exposure Basis

## Broker quantity sources traced

| Source | Used by, before | Used for exposure, now |
|---|---|---|
| `holdings()[].quantity` | risk (only this), reconciliation, `get_held_quantity`, dashboard | settled shares |
| `holdings()[].t1_quantity` | reconciliation, `get_held_quantity` — **not** risk | added |
| `holdings()[].collateral_quantity` / `used_quantity` / `realised_quantity` | snapshot tables only | not added (see semantics) |
| `positions()["net"]` | reconciliation, `get_held_quantity` (all products), sector "unmapped" list — **not** risk | CNC on NSE/BSE: `quantity − overnight_quantity` |
| `holdings()[].last_price` / `average_price` | risk: MTM denominator, cost numerator | `last_price` (else `close_price`) for both |
| `margins()["equity"]["net"]` | risk denominator | `net − available.collateral` |
| PK-13 `filled_quantity` / open quantity / risk quantity | same-day app tickets on top of holdings | the app side of `max(broker, app)` plus what may still fill |

## Holdings semantics

- **`quantity`** is settled demat shares. A sale made today is **not** deducted from it: Kite shows it as a negative CNC position, and the holding's `used_quantity` rises.
  - The app already relied on this convention in `reconciliation._check_position_state` and `get_held_quantity`, which sum holdings and positions.
- **`collateral_quantity`** (pledged) is treated as part of `quantity`. Pledged shares stay in the demat account and are still owned, so it is not added again.
  - This could not be verified against a live payload (see Known Limitations).

## T1 semantics

`t1_quantity` is bought but not yet delivered. Kite's holdings documentation lists it separately from `quantity`, and it is **not** included in it, so the two are **added**. This is the same formula reconciliation and `get_held_quantity` have used since PK-7.

Effect: yesterday's buy is exposure today (test 2, and test 8, the day-2 scenario). It is also part of the portfolio value, which it was not before.

## Positions semantics

- **Included:** only delivery positions (`product == "CNC"`, exchange NSE or BSE).
  - Their contribution is **today's change**: `quantity − overnight_quantity`.
  - A CNC position's overnight quantity is yesterday's buy, which is already the holding's T1. Using the net `quantity` would count it twice (test 7).
- **Left out:** MIS (intraday, squared off the same day) and NRML/derivatives. They are not delivery exposure, and this app only trades CNC (test 7).
- **Per symbol:** `quantity = max(0, settled + t1 + day change)`.
- **Scope.** Exit validation (`get_held_quantity`) and reconciliation keep their broader all-product sums **on purpose**: narrowing an exit path was out of scope.

## App-ticket interaction and double-count prevention

`account_exposures()` makes one SQL pass per approval, computing per symbol (live only, excluding the ticket being checked):

- **`app_held`** = Σ over open positions (`_OPEN_POSITION_WHERE`, the same definition as the position count) of `max(0, COALESCE(open quantity, risk quantity))`: the shares PK-13 says the app holds. If unknown, it counts what the position may hold.
- **`pending`** = the unfilled rest of each working entry (`risk quantity − held`), plus **every** in-flight, UNKNOWN or needs_review entry at full quantity, **whatever its age**.

Then:

- **Held shares = `max(broker quantity, app_held)`.** Broker and app describe the same economic position, so they are never added:
  - app 40 plus broker 40 → **40** (test 5);
  - app 40 plus broker 140 → **140**, the external 100 on top (test 5b);
  - app 40 plus broker 10 (the fill not reflected yet) → **40** (test 5c).
- **Value** = held × broker `last_price` (the app's average entry price only if the broker has no price for the symbol), plus pending × planned entry price.

The **"same-day ticket workaround"** (adding only today's executed tickets on top of holdings, with an IST-midnight cutoff) is **removed**.

- It existed because holdings missed today's buys. `positions()` now provides them, and `max()` covers any lag.
- It also missed yesterday's buy entirely, which was the P1-5 (a) defect.
- The in-flight cutoff is gone too. An UNKNOWN entry from yesterday is still counted, consistently with the open-position count and open risk, which never had a cutoff.

## UNKNOWN and in-flight entries

These are preserved conservatively. An UNKNOWN entry counts at full quantity until `order_monitor` resolves it (test 6; test 6 also covers two days old).

If the broker already shows its fill, it may be counted twice for that moment (test 6b). It is **never zero**. Once the monitor records the fill, the entry becomes an open position, and `max()` counts it once.

## Portfolio-value basis

`portfolio_value = Σ broker quantity × last_price + (equity.net − equity.available.collateral)`.

- **Pledged collateral.** Kite documents `equity.net` as the net balance available for trading, *including* collateral. The pledged holdings are already counted at market value, so their collateral margin is taken out once. A missing `available.collateral` counts as 0: the previous behaviour, and FakeKite's default.
  - Example: 300,000 of pledged holdings plus `net` 500,000 (of which 200,000 is collateral) gives **600,000, not 800,000** (test 11).
- **T1 shares and today's CNC positions** are part of the value. Before, the cash debit of a fresh buy made the portfolio temporarily look smaller.
- **Fail closed:** a portfolio value that is not positive or not finite raises `PORTFOLIO_EXPOSURE_UNAVAILABLE` (see Fail-Closed Behavior).

## Cost vs MTM decision

**Current market value / current portfolio value**, as the brief preferred.

- **Held shares:** broker `last_price`. The denominator was already MTM, so now both sides agree.
- **Shares not yet held** (pending, UNKNOWN, the proposed trade): planned entry price, the only price they have.
- **`compute_suggested_exit_quantity`** (Quick Action concentration popup) follows the same basis:
  - it sizes the sale at `last_price`;
  - `quantity_held` is the broker quantity (settled + T1 + today);
  - the response adds `last_price` and `exposure_basis: "market_value"`;
  - the popup label now says "market value".

Consequence: a winning position now looks as large as it is, so there is less room to add to it. A losing one looks smaller. Concentration risk is about what the position is worth now.

## External positions

A holding, T1 lot or same-day CNC buy made outside the app **counts in full** without any ticket (tests 3 and 13). This covers the single-stock limit, the sector sum and the portfolio value.

## Position count

**Unchanged.** `max_open_positions` counts the app's open positions plus in-flight entries (test `test_max_open_positions_counts_app_positions_not_external_holdings`).

- Counting external holdings would change what the limit means. The trading account may hold long-term shares, and a user with 12 such holdings could never open a trade.
- External exposure is governed by the single-stock, sector and portfolio-value checks instead.
- PK-13 partial positions still count as one position each.

## PK-13 interaction

- **Requested 100, filled 40, entry ended:** 40 (test 4).
- **Still working:** 100 = 40 held + 60 pending (test 4b), per PK-13 decision (a).
- **Broker already shows the 40:** still 40 (test 5).
- **Unchanged:** PK-13's open risk (`_open_risk_sum`) and `list_open_positions`.
- **Restated helper:** `test_partial_fills`' `_risk` helper now passes a context with an empty `broker_by_symbol`, instead of the removed `holdings_by_symbol`. Every PK-13 exposure expectation (100 → 40 → 25 → 60) holds unchanged.

## PK-12 interaction

| PK-12 control | Effect |
|---|---|
| 12C daily entry count, trading lock, 12H AMO consent, 12B absolute max notional, 12G broker breaker | **Unchanged.** DB and request only; they never read the portfolio. |
| 12F daily-loss % | Same code, **new denominator**: `portfolio_value` now includes T1 and today's CNC and excludes collateral. The limit is a percentage of the corrected figure. |
| Dry run in live exposure | Still excluded (`mode = 'live'` everywhere; test 12 and the PK-14 dry-run test). |
| 12F "unusable portfolio value" | A non-positive value is now refused one step earlier, in risk revalidation (`RISK_REVALIDATION_UNAVAILABLE`, whose message contains `PORTFOLIO_EXPOSURE_UNAVAILABLE`), and at creation too. It used to pass every percentage check as 0 % and reach the 12F gate. The 12F guard itself is unchanged and still tested. |

The same denominator also changes position sizing (`compute_position_size`), the journal's starting-equity fallback and the backtest's starting equity. All three were already "today's portfolio value".

## Tests (`tests/test_exposure_basis.py`, 27 including parametrizations)

| Brief # | Test |
|---|---|
| 1 | `test_1_settled_holdings_count_at_market_value` |
| 2 | `test_2_t1_quantity_counts_as_holdings_and_portfolio_value` |
| 3 | `test_3_same_day_external_cnc_buy_counts` |
| 4 | `test_4_app_partial_fill_counts_what_filled_once_the_entry_ended`, `test_4b_working_partial_fill_counts_the_requested_size` |
| 5 | `test_5_app_position_already_at_the_broker_is_counted_once`, `test_5b_external_shares_on_top…`, `test_5c_filled_app_shares_the_broker_has_not_shown_yet_still_count` |
| 6 | `test_6_unknown_entry_counts_in_full_whatever_its_age`, `test_6b_…never_zero_even_if_the_broker_already_filled_it` |
| 7 | `test_7_one_symbol_across_holdings_t1_and_positions` (negative CNC sale, overnight CNC, MIS, NFO) |
| 8 | `test_8_yesterdays_buy_gets_no_fresh_single_stock_allowance` (T1 and the app ticket are one lot: 9.9 % → 18.9 % refused) |
| 9 / 17 | `test_17_t1_holding_same_day_position_and_new_entry_share_one_sector_total` (13 % → 19 % passes; 20.5 % refused) |
| 10 | test 1: 90,000 at market, not 60,000 at cost |
| 11 | `test_11_pledged_collateral_margin_is_not_counted_twice` |
| 12 | `test_12_dry_run_tickets_never_count` |
| 13 | `test_13_external_holding_without_any_ticket_is_in_the_risk_check` |
| 21 | `test_broker_snapshot_is_one_call_each` (holdings, positions, margins: one call each per approval) |
| 22 | `test_broker_data_unavailable_refuses_a_new_entry_at_creation` ×3; `test_unusable_portfolio_data_is_portfolio_exposure_unavailable` ×4; `test_positions_failure_at_confirmation_sends_nothing`; `test_exits_never_need_the_portfolio_snapshot` |
| 15 | `test_max_open_positions_counts_app_positions_not_external_holdings` |

**Existing tests restated.** Each carries a comment. Nothing was weakened, and each asserts more than before.

| Test | Was | Now |
|---|---|---|
| `test_baseline_api::test_sector_mappings` | unmapped list = `["RELIANCE"]` (held, lookup-covered, not seeded) | `[]`; an unknown holding NOSUCHCO **is** listed |
| `test_alert_action::test_position_reduction_suggests_quantity_to_hit_target_exposure` | cost basis, "0 < qty ≤ 100" | MTM basis, exact portfolio value 1,133,000, exact exposure, **exactly 6** shares |
| `test_account_safety::test_12f_unusable_portfolio_value_fails_closed_when_pct_is_enabled` | 503 `ACCOUNT_SAFETY_UNAVAILABLE` at the 12F gate | 503 `RISK_REVALIDATION_UNAVAILABLE` / `PORTFOLIO_EXPOSURE_UNAVAILABLE` one step earlier (the ticket is created before the value breaks, since creation refuses too); no order; the 12F guard is asserted directly |
| `test_partial_fills::_risk` (helper) | `SimpleNamespace(holdings_by_symbol={})` | `SimpleNamespace(broker_by_symbol={})` |

**FakeKite** (`tests/fake_kite.py`) changes:
- `set_position` also reports `last_price`, `average_price` and `overnight_quantity`, as Kite does; prices default to the LTP;
- `margins_collateral` adds `equity.available.collateral` only when set, so every existing payload is byte-identical.

# Shared Portfolio Context

`build_portfolio_context(kite)` makes **one** `holdings()`, **one** `positions()` and **one** `margins()` call, and returns `PortfolioContext(portfolio_value, broker_by_symbol, cash_component, collateral_excluded)`.

- **Within `run_checks`,** a single `account_exposures` map, built from that context and one SQL pass, feeds both the single-stock and the sector check. All percentage checks divide by the same `portfolio_value`.
- **At confirmation,** `_revalidate_at_confirm` builds the context once and hands it to the PK-12 final gate (unchanged since PK-12).
- **The strategy scan** keeps its one-context-per-batch.

The net broker cost per approval is **+1 call** (`positions()`).

# Fail-Closed Behavior

| Condition | New entry | Exit / Emergency Exit / protection / reconciliation |
|---|---|---|
| `holdings()` / `positions()` / `margins()` raises a Kite error | creation: 502 `KITE_API_ERROR` (401 `SESSION_EXPIRED` for a token); confirmation: 503 `RISK_REVALIDATION_UNAVAILABLE`, ticket `rejected` (Revert-able), nothing sent | unaffected: a closing sell never builds the context (test with `positions`/`margins` down) |
| malformed payload (missing `tradingsymbol`, non-numeric margin) | 503 `PORTFOLIO_EXPOSURE_UNAVAILABLE` | unaffected |
| a held symbol with no market price (`last_price`/`close_price` ≤ 0 or missing) | 503 `PORTFOLIO_EXPOSURE_UNAVAILABLE` | unaffected |
| portfolio value ≤ 0 or not finite | 503 `PORTFOLIO_EXPOSURE_UNAVAILABLE`, both in `build_portfolio_context` and as a guard in `run_checks` for a context passed in | unaffected |
| symbol with no sector | `risk_rejected` / 409, `SECTOR_MAPPING_REQUIRED` | unaffected |

# Multi-Account Future Compatibility

Nothing multi-account was built. The assumptions are:

- Every figure comes from **the trading account's own** `kite` session and `account_id`: one `PortfolioContext` per call, from the Kite client of the account being checked.
- Concentration limits and risk capital are **this account's** holdings, positions and cash. The two investment-only family accounts are not, and must not be, part of them.
- A future family-portfolio view should be a separate aggregation. It must not feed `build_portfolio_context`.
- Nothing in PK-14/15 consolidates across accounts:
  - `sector_mapping` is a global symbol → sector table. It is a classification, not holdings, so sharing it across accounts is correct.
  - `seed_known_symbols` reads every account's watchlist, which is harmless for the same reason.

# Files Changed

| File | Change |
|---|---|
| `backend/sector_mapping.py` | **PK-14.** Lookup fixes, additions and a taxonomy comment; `seed_symbols`, `seed_known_symbols`, `resolve_sectors`; `get_sector` resolves through the lookup; `seed_from_holdings` also takes positions; `find_unmapped_symbols` includes the watchlist and resolves. |
| `backend/risk_engine.py` | **PK-15:** `PortfolioContext(broker_by_symbol, cash_component, collateral_excluded)`, `broker_quantities`, a fail-closed `build_portfolio_context`, `PORTFOLIO_EXPOSURE_UNAVAILABLE`, `SymbolExposure` / `account_exposures` (replacing the same-day workaround), `compute_suggested_exit_quantity` on MTM, and the `run_checks` portfolio-value guard. **PK-14:** `CheckResult.code` / `current_value`, `SECTOR_MAPPING_REQUIRED`, and the sector check over resolved sectors. **Both:** the single-stock and sector checks take the shared exposure map, `_PCT_EPSILON`. |
| `backend/main.py` | `seed_known_symbols()` at startup, after `init_db()`. |
| `backend/routes/watchlist.py` | Seeds the sector on add. |
| `backend/routes/tickets.py` | `failed_codes` in the confirm 409 (1 line). |
| `frontend/app.js` | Risk-check row (current → post-trade, UNMAPPED — BLOCKED, fix link), unmapped empty-state text, "Exposure (market value)". |
| `frontend/index.html` | Two Settings captions. |
| `tests/fake_kite.py` | `set_position` prices and `overnight_quantity`; `margins_collateral`. |
| `tests/test_sector_exposure.py` | New, 22 tests (PK-14). |
| `tests/test_exposure_basis.py` | New, 27 tests (PK-15 and combined). |
| `tests/test_baseline_api.py`, `test_alert_action.py`, `test_account_safety.py`, `test_partial_fills.py` | Restated (see the PK-15 Tests section). |
| `docs/OPUS55_PK14_PK15_IMPLEMENTATION_REPORT.md` | This report. |

The following were **not** changed: `docs/OPUS55_AUDIT_REPORT.md`, `backend/db.py` (no schema change), `get_held_quantity`, reconciliation, `order_monitor`, the trailing engine, and the kill switch.

# Database Changes

**No schema change and no migration.** The only data change is `sector_mapping` rows added by seeding. These happen at startup (`seed_known_symbols`), after login and on watchlist add, and always as `INSERT OR IGNORE`.

**Validation.** `db_check.py` (scratchpad) made an online-backup copy of `kite_terminal.db.pre_pk14_pk15_bak` (opened `immutable=1`) into the scratchpad. It pointed `db.DB_PATH` at the copy, used a throwaway encryption key and `.env`, and ran `init_db()` + `sector_mapping.seed_known_symbols()` **twice**.

| | before | run 1 | run 2 |
|---|---|---|---|
| `integrity_check` | ok | ok | ok |
| `foreign_key_check` violations | 0 | 0 | 0 |
| `sector_mapping` rows | 1 | 52 | 52 |
| rows added by the seed | — | 51 | **0** |

- **Row counts.** Every other table is preserved. The only other differences are `account_safety_state` (+1 seeded row) and the empty `broker_failure_log`. These are **PK-12's own pending migrations**: the live DB has not been started under PK-12+ code yet, and the PK-13 report noted the same.
- **Run 2 is identical to run 1:** schema, counts and every sector row.
- **Manual mapping preserved:** ATHERENERG is still "Consumer Durables".
- **Coverage:** every live watchlist symbol now has a row. The seeded set is the 50 watchlist symbols, plus AWL from ticket history, each exactly as in the taxonomy table.
- **Live DB untouched:** SHA-256 `1378f8b47cd7…` and mtime unchanged.
  - The backup's read-only open at the very start refreshed the live DB's `-shm` (WAL shared-memory index, not data); its `-wal` stayed 0 bytes.
  - The backup file itself is SHA-256 `01a9904837d2…`.

# Mutation Results

`pk14_15_mutations.py` (scratchpad, not committed) ran once on the final code. For each mutant it:

1. applied one exact-anchor edit, asserting the anchor was unique;
2. ran the 5 targeted files (`test_sector_exposure`, `test_exposure_basis`, `test_partial_fills`, `test_alert_action`, `test_baseline_api`);
3. restored the original bytes.

| # | Mutation | Result |
|---|---|---|
| M1 | unknown sector passes instead of blocking | **caught**, 3 (tests 7, 7b, 7c) |
| M2 | proposed trade omitted from sector exposure | **caught**, 7 (tests 4, 5, 6, 9, 10, 17, …) |
| M3a | watchlist add no longer seeds the sector | **caught**, 1 (test 3) |
| M3b | startup seeding skips the watchlist | **caught**, 2 (tests 1, 11) |
| M4 | `t1_quantity` removed | **caught**, 4 (tests 2, 7, 8, 17) |
| M5 | `positions()` ignored | **caught**, 7 (tests 3, 4b, 5, 6b, 7, 17, …) |
| M6 | app position + broker holding double counted (`+` instead of `max`) | **caught**, 6 (tests 4b, 5, 5b, 5c, 8, 17) |
| M7 | requested instead of the actual PK-13 quantity | **caught**, 4 (tests 4, 4b, 5 and PK-13's `test_4_risk_counts_requested…`) |
| M8 | (extra) pledged collateral counted twice | **caught**, 1 (test 11) |
| M9 | (extra) non-positive portfolio value accepted by `build_portfolio_context` | **caught**, 1 (`…[zero_value]`). The HTTP paths stay closed even under this mutant: `run_checks` has its own guard, so only the direct unit test sees it. |

Restored = True; survivors: none. SHA-256 after restore, identical to before:

- `risk_engine.py` `fe758a749873…`
- `sector_mapping.py` `af55d6ace21b…`
- `routes/watchlist.py` `70ca8b61fc17…`

No code changed after the mutation run; only this report was written afterwards.

# Targeted Repeats

`pk14_15_repeats.py` (scratchpad) ran on the final code, after the mutation run.

- **Combined PK-14/15 targeted suite ×10:** every run **377 passed**, 0 failed (51–118 s). It covers:
  - `test_sector_exposure`, `test_exposure_basis`, `test_partial_fills`, `test_alert_action`, `test_baseline_api`;
  - `test_live_safety`, `test_account_safety`, `test_account_safety_integration`;
  - `test_emergency_exit_isolation`, `test_pk9_pk11_cross`.
- **Exposure/race subset ×20:** every run **69 passed**, 0 failed (≈21 s). It runs `-k "race or racing or concurrent or exposure or sector or monotonic"` over the new files, `test_partial_fills`, `test_live_safety`, `test_account_safety_integration` and `test_pk9_pk11_cross`, covering the PK-1/2/4/11/12C/13 races and every exposure/sector test.

PK-14/15 add no concurrency of their own: the context is read-only, and seeding is `INSERT OR IGNORE`. The subset guards against the new exposure SQL disturbing the existing claim and revalidation races.

No flakes were seen. Runs 9–10 of the targeted suite took about twice as long as runs 1–7, with identical results, which fits machine load rather than a hang.

# Full Suite ×3

These are three consecutive runs on the final code, after the mutation run and the repeats (`full_x3.py`, scratchpad, `-rfEX`).

| Run | passed | xfailed | failed | xpassed | time |
|---|---|---|---|---|---|
| 1 | 718 | 0 | 0 | 0 | 212.3 s |
| 2 | 718 | 0 | 0 | 0 | 207.0 s |
| 3 | 718 | 0 | 0 | 0 | 209.5 s |

718 = 669 (baseline) + 22 (PK-14) + 27 (PK-15). The restated tests keep their counts.

# Known Limitations

1. **Kite payload semantics were not verified against a live account in this session.** No live Kite call was made. The following come from Kite Connect's documentation and the conventions already established in the codebase:
   - `t1_quantity` is separate from `quantity`;
   - a same-day sale shows as a negative CNC position;
   - CNC overnight quantity equals the T1 holding;
   - `equity.net` includes `available.collateral`.

   If Kite ever deducted a same-day sale from `quantity` as well, the sum would under-count by that sale (floored at 0 per symbol). If `quantity` excluded pledged shares, pledged holdings would be under-counted. A read-only look at one real `holdings()`/`positions()`/`margins()` payload with a T1 lot and a pledge would settle both.
2. **UNKNOWN entries may be counted twice** for the moment between the broker filling them and `order_monitor` recording the fill. This is conservative by design, and never zero.
3. **A lagging broker combined with external shares.** If the broker has not reflected an app fill and the account also holds external shares of that symbol, `max()` may under-count until `positions()` catches up. For example, 460 external plus 40 new gives 460, not 500. Kite's positions update with the fill, so the window is small.
4. **Banking and Financial Services stay separate sectors.** A mixed bank/NBFC position can reach twice the sector limit in "financials" combined. This is the existing taxonomy, kept as the brief asked. Merging them is a one-line change if wanted.
5. **Unmapped holdings count toward no sector.** They are named in the check message and listed in Settings, but a held unknown IT name does not raise the IT total until it is mapped.
6. **The sector list is static.** A new listing or a renamed symbol needs a manual mapping (one click in Settings) before it can be traded. That is the intended fail-closed cost.
7. **The dashboard's "Portfolio Value" differs from the risk engine's.** The dashboard's figure (holdings `quantity` × LTP + `net`) is unchanged, because the PK-19 redesign is out of scope. The risk engine's figure now includes T1 and today's CNC, and excludes collateral.
8. **Manual non-closing SELL tickets** still add their notional to exposure, as before. The PK-17 manual BUY+CNC restriction is not started.

# Deferred Work

These are dependencies only; nothing was implemented.

- **PK-16:** orphan-GTT reverse reconciliation.
- **PK-17:** the manual BUY+CNC restriction, including how a manual SELL relates to exposure.
- **PK-18:** security and cloud hardening.
- **PK-19:** a lifecycle UI, the dashboard's portfolio value, and a richer risk-detail layout.
- **PK-20** to **PK-28**, and the multi-account portfolio architecture.
- **Possible follow-ups:**
  - verify the Kite payload semantics against one real account snapshot (limitation 1);
  - decide whether Banking and Financial Services should merge (limitation 4);
  - decide whether ATHERENERG's manual "Consumer Durables" mapping should become Auto.

# Deviations From Audit

1. **Fail closed, not an "Unmapped" bucket.** The audit offered either option; the brief preferred fail-closed.
2. **`max(broker, app)` rather than "add `positions()` quantity".** The audit's correction ("include `t1_quantity` … add the `positions()['net']` quantity") would double count the app's own tickets on top of the broker's. The same-day workaround was removed, and the app side became a floor rather than an addend.
3. **CNC only, today's change only.** The audit said "positions net for CNC". PK-15 also subtracts `overnight_quantity`, so yesterday's CNC buy is not counted twice with its T1 holding.
4. **Cost basis became market value** (P1-5 (d)). The audit called the old mix "documented and deliberate"; the brief preferred MTM.
5. **Collateral** is removed from `equity.net`, rather than left as a documented risk (P1-5 (c)).
6. **A non-positive portfolio value is now an error.** It used to silently pass every percentage check at 0 %. This was not in the audit; it was found while making the denominator fail-closed.
7. **The in-flight exposure cutoff was removed.** UNKNOWN entries of any age count, consistently with the open-position count and open risk. It was not listed in the audit.
8. **The lookup was extended,** and the `INDUS TOWERS` key fixed. The audit only asked for seeding. Without the extension, fail-closed would have blocked 15 of the 51 live watchlist symbols.
