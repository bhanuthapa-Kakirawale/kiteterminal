# DMB → Kite: source-rights matrix

**Status on 2026-11-03: BLOCKED.** No written permission exists for any real-data source in any of the four use categories below. The technical controls that enforce this are unchanged:
- DMB `private_desk/handoff/rights.py`: every real source is `UNRESOLVED`; a real export is `RIGHTS_BLOCKED` (exit 20) and writes no bundle.
- DMB `publication/rights.py`: `PUBLIC_REVIEW_REQUIRED_POLICY=BLOCK`.
- Kite `backend/integration_profile.py`: `PRODUCTION_PERMITTED_SOURCES = frozenset()`, so only synthetic bundles inside the isolated test profile are actionable.

No `publication_rights` or handoff status was changed, no source was relabelled, and no real production evidence was exported in this sprint.

**This is an operating record, not legal advice.** It states what evidence exists and what is missing. It does not interpret any provider's terms; that is for the owner and, if needed, counsel.

## 1. The four use categories (each needs its own permission)

| # | Category | What it means here |
|---|---|---|
| 1 | **Private internal use** | DMB collecting, storing and showing a source's data to its owner, on the owner's own machine, for research (DMB Private Desk). |
| 2 | **Private cross-application transfer** | DMB copying data derived from that source into a *second* application (Kite Terminal), which stores it in its own database and shows it next to brokerage data and an order ticket. |
| 3 | **Public publication** | The YouTube Short (or any public post) showing that data or anything derived from it. |
| 4 | **Commercial redistribution** | Giving or selling the data, or derivatives of it, to others (subscribers, clients, an API). |

Permission in one category does not imply another. In particular:
- reachability (an endpoint answers) is not permission;
- a paid subscription (e.g. Kite Connect) is not permission to redistribute or to display on another platform;
- attribution ("Source: NSE") does not cure a restriction;
- `PRIVATE_RESEARCH_ONLY` is a classification, not a permission (`classification_is_permission: false` in every packet).

## 2. Matrix

The real packet depends on every DMB source below (`REAL_PACKET_SOURCES`, deliberately over-inclusive: an extra unresolved dependency can only block). "Evidence available" means evidence recorded in either repository; none of these terms documents was retrieved or reviewed in this sprint.

| Source (registry name) | Data fields used in the packet | Collection method | Storage / retention | 1 Internal display | 2 Cross-app transfer to Kite | 3 Public | 4 Commercial | Permission or licence needed | Evidence available | Final status |
|---|---|---|---|---|---|---|---|---|---|---|
| **Yahoo Finance** (`yahoo_finance`) | Daily OHLCV of NIFTY 200 stocks and indices (`^NSEI`, `^INDIAVIX`, sectors): close, `price_change_pct`, `relative_volume`, `rel_5d_pp`, `rel_20d_pp`, detector replay values, market context (NIFTY change, India VIX) | `yfinance` (an unofficial client of Yahoo's web endpoints; no API key, no contract) | DMB OHLCV store and canonical reports (SQLite + JSON, indefinite); Kite DB copy of packet fields when imported (indefinite) | **Unresolved** (in use privately today; no reviewed basis recorded) | **UNRESOLVED → BLOCKED** | BLOCKED (`REVIEW_REQUIRED` → `BLOCK`) | Not permitted (no basis) | Yahoo's terms for automated retrieval, storage and private reuse; a licence if the terms do not allow it | None recorded | **BLOCKED** |
| **NSE website / APIs** (`nse_website`) | Index facts, official lists | HTTPS requests to nseindia.com JSON endpoints (no API contract) | DMB snapshots/reports (indefinite) | Unresolved | **UNRESOLVED → BLOCKED** | BLOCKED | Not permitted | NSE's website terms of use for automated access; an NSE data licence (NSE's data and index licensing arm) for reuse/redistribution | None recorded | **BLOCKED** |
| **NSE index close archive** (`nse_index_close_archive`) | Benchmark gap / recap-session recovery (index close) | Download of `ind_close_all_DDMMYYYY.csv` from nsearchives | Reports (indefinite) | Unresolved | **BLOCKED** | BLOCKED | Not permitted | As NSE above; index data may be NSE Indices Ltd.'s | None recorded | **BLOCKED** |
| **NSE / Nifty index constituents** (`nse_index_constituents`) | Universe: symbol, company name, sector (Industry column) | Download of `ind_<index>list.csv` (nsearchives / niftyindices.com) | DMB universe files (indefinite) | Unresolved | **BLOCKED** | BLOCKED | Not permitted | NSE Indices Ltd. terms for constituent data | None recorded | **BLOCKED** |
| **NSE F&O ban list** (`nse_fo_secban`) | Ban-period flag/context | `fo_secban.csv` download | Official snapshots (immutable, indefinite) | Unresolved | **BLOCKED** | BLOCKED | Not permitted | NSE terms | None recorded | **BLOCKED** |
| **NSE surveillance (ASM/GSM)** (`nse_surveillance`) | Surveillance-stage context | NSE website API | Official snapshots (indefinite) | Unresolved | **BLOCKED** | BLOCKED | Not permitted | NSE terms | None recorded | **BLOCKED** |
| **NSE IPO lists** (`nse_ipo_issues`) | Issue context | NSE website API | Official snapshots (indefinite) | Unresolved | **BLOCKED** | BLOCKED | Not permitted | NSE terms | None recorded | **BLOCKED** |
| **DMB derived** (`daily_byte_derived`) | Reason codes, attention level, novelty, persistence, quality, regime | Computed by DMB from the sources above | DMB stores; Kite DB when imported | Inherits sources | **BLOCKED (inherits)** | BLOCKED (inherits) | Not permitted | The underlying sources' permissions (derivation does not launder rights) | — | **BLOCKED** |
| **DMB Market Structure** (`daily_byte_market_structure`) | Sector rows, breadth context | DMB aggregate over NSE/Yahoo data | Immutable revisions (indefinite) | Inherits | **BLOCKED (inherits)** | BLOCKED | Not permitted | As above | — | **BLOCKED** |
| **Zerodha Kite Connect** (Kite Terminal only; not a DMB source) | Holdings, positions, orders, margins, quotes, instruments, historical candles (Kite's own `ohlc_daily`) | Kite Connect API under the owner's paid app and daily login | Kite DB (indefinite; access token Fernet-encrypted) | Personal use by the account holder under the Kite Connect subscription. Not re-reviewed this sprint | **Not applicable to DMB → Kite** (data flows from DMB into Kite, not out of Kite). Showing DMB research next to Kite Connect data in a personal app: unreviewed | **Not permitted / not assumed.** A subscription is not a redistribution licence | Not permitted | Kite Connect terms of use and the exchange data policies they pass through, for (a) storing historical/market data locally, (b) displaying it alongside third-party research | None recorded | **Personal dry-run use only; nothing else assumed** |
| **Synthetic fixture** (`synthetic_handoff_fixture`) | Everything in a synthetic packet | Generated by DMB's tests | Scratch folders only | Permitted | **PERMITTED** (only inside Kite's isolated synthetic test profile) | n/a | n/a | none: no third-party content | `rights.py` registry | **PERMITTED (test only)** |

Sources DMB uses that are **not** in the packet (and are kept out by tests): Gemini (AI; never a source of truth), Google News RSS, CDSL, NSDL, RBI, NSE IX/GIFT Nifty (`RESTRICTED`), SEBI offer documents. They do not reach `PrivateCandidatePacket` (DMB test-enforced). Each would need its own row before it ever could.

## 3. Evidence the owner needs to obtain, per provider

Get each item **in writing** (a signed licence, an email from the provider's licensing/legal contact, or the exact clause of current terms you have accepted, saved with its date and URL). Record it in DMB's `private_desk/handoff/rights.py` basis text and here. Then change one source at a time, with a reviewed commit.

| Provider | Ask for written confirmation (or a licence) that covers | Who to ask |
|---|---|---|
| **Yahoo** | (1) automated retrieval of Yahoo Finance quote/history data by a script for personal research; (2) storing it locally beyond a session; (3) copying data derived from it into a second personal application; (4) public display of derived figures in videos; (5) any commercial use. If Yahoo does not license this, replace the source with a licensed feed. | Yahoo's legal / data licensing contact, or a licensed market-data vendor |
| **NSE / NSE Indices Ltd.** | For each dataset (website JSON APIs, index close archive, index constituents, F&O ban, ASM/GSM, IPO lists): (1) permission for automated access to nseindia.com / nsearchives; (2) local storage; (3) internal transfer to a second personal application; (4) public display (with any attribution or delay conditions); (5) redistribution. Index data (Nifty values, constituents) may need NSE Indices Ltd.'s own licence. | NSE data licensing / market data products; NSE Indices Ltd. for index data |
| **Zerodha (Kite Connect)** | (1) that storing Kite Connect historical and quote data in the terminal's local database is within the Kite Connect terms for a personal app; (2) that showing third-party research (DMB packets) next to Kite data in that app is allowed; (3) confirmation that the subscription grants **no** redistribution or public display right, so this is never assumed. | Kite Connect support / Zerodha compliance |
| **Owner (internal record)** | A dated decision, per source and per category, that the evidence above was reviewed; who reviewed it; and the scope (e.g. "private cross-app transfer only, no public use"). | — |

## 4. What stays true until then

- **Category 1 (internal):** DMB's private desk continues as it is today. That is an existing owner decision, and this matrix records that its basis is also unresolved.
- **Category 2 (cross-app):** **BLOCKED.** No real bundle is exported (`RIGHTS_BLOCKED`), and Kite would refuse one (`RIGHTS_NOT_APPROVED`; empty production allowlist).
- **Category 3 (public):** **BLOCKED** (`PUBLIC_REVIEW_REQUIRED_POLICY=BLOCK`).
- **Category 4 (commercial):** not contemplated and not permitted.
- The synthetic fixture is the only permitted source, and only inside the isolated test profile.
