# DMB → Kite: source-rights matrix

**Status on 2026-11-04: BLOCKED (re-reviewed against the providers' official terms, §5).** No written permission exists for any real-data source in any use category. The terms retrieved on 2026-11-04 expressly prohibit automated collection from Yahoo and NSE without permission, so even Category 1 (private internal use) is not supported by documented rights. The technical controls that enforce this are unchanged:
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

## 5. Evidence review of 2026-11-04 (official terms retrieved)

Retrieved 2026-11-04 from each provider's own site. Quotations are verbatim. This is still an operating record, not legal advice. **No status in DMB `rights.py`, DMB `publication/rights.py` or Kite `integration_profile.py` was changed.** The review found nothing that permits any category, and it found express prohibitions that affect Category 1 too.

### 5.1 Documents

| Provider | Document | Link | Version / date | Clauses relied on |
|---|---|---|---|---|
| Yahoo (Yahoo Finance via `yfinance`) | Yahoo Terms of Service | https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html | US version dated 15 May 2025 (as shown on the page, retrieved 2026-11-04) | §2(d)(9): you may not "access or collect data … from our Services using any automated means, devices, programs, algorithms or methodologies, including but not limited to robots, spiders, scrapers, data mining tools, or data gathering or extraction tools, for any purpose without our express, prior permission." §2(d)(10): no use of any data "to create any database, archive … data feed … or any other aggregated data source that competes with or constitutes a material substitute for the Services". §2(e): "Unless otherwise expressly stated, you may not access or reuse the Services, or any portion thereof, for any commercial purpose." §2(h): no reproduce / distribute / transmit / broadcast / derivative works / commercial exploitation. |
| NSE (nseindia.com, nsearchives) | NSE Terms of Use | https://www.nseindia.com/static/nse-terms-of-use | No date on the page; retrieved 2026-11-04 | "User is prohibited to conduct any systematic or automated data collection activities (including scraping, data mining, data extraction and data harvesting) on or in relation to our Website / Mobile Application." Content may not be "copied … reproduced, uploaded, transmitted, posted, stored (either in hardcopy or in an electronic retrieval system) … disseminated, distributed, displayed … published … in any form, without prior written permission of NSE. Unless the information or Content is available for download, not to aggregate, copy or duplicate in any manner…" Content is "the exclusive property of NSE". |
| NSE (licensing route) | NSE Data Sharing & Usage Policy | https://www.nseindia.com/static/market-data/nse-data-policy | "Updated on: 11/12/2025" | Market data is licensed to Subscribers (data vendors, media, non-commercial users, research entities…) through NSE Data and Analytics Ltd. under a "Relevant Agreement"; for non-commercial users "the limitations on such use of data … shall be clearly set out in the underlying documentation". This is the documented route to a permission. No such agreement exists. |
| Zerodha | Kite Connect Terms & Conditions | https://kite.trade/terms | No version or date on the page; retrieved 2026-11-04 | §2a: "live market data obtained via Kite Connect cannot be displayed to the public at large"; no use "for virtual/mock trading apps, trading related games, to mislead or deceive others"; "a limited license provided by Zerodha to You, for use within India". §4b: you will not "Scrape, build databases, or otherwise create permanent copies of such content, or keep cached copies with the intent of redistributing", nor "Copy, translate, modify, create a derivative work of, sell, lease, lend, convey, distribute, publicly display, or sublicense to any third party." |

### 5.2 Permission by use (six separate permissions)

Key: **PROHIBITED** = the provider's terms expressly forbid it without permission, and none is held. **NOT EVIDENCED** = no document grants it. **AMBIGUOUS** = the terms can be read either way. **PERMITTED** = a document grants it.

| Source | 1 Personal/private research in DMB | 2 Internal storage and processing | 3 Transfer DMB → Kite | 4 Display/use in Kite | 5 Public (video, website) | 6 Commercial redistribution | Evidence sufficient? |
|---|---|---|---|---|---|---|---|
| **Yahoo Finance** (`yahoo_finance`, collected by `yfinance`) | **PROHIBITED** as collected: automated collection "for any purpose without our express, prior permission" (§2(d)(9)) | **PROHIBITED** (follows from 1; plus §2(d)(10) on databases) | **PROHIBITED / NOT EVIDENCED** | **NOT EVIDENCED** | **PROHIBITED** (§2(h)) | **PROHIBITED** (§2(e), §2(h)) | Yes, sufficient to block. The only cure is Yahoo's express prior permission or a licensed feed. |
| **NSE website / JSON APIs** (`nse_website`, `nse_surveillance`, `nse_ipo_issues`) | **PROHIBITED** as collected (automated collection clause) | **PROHIBITED** without written permission ("stored … in an electronic retrieval system") | **PROHIBITED** without written permission | **PROHIBITED** without written permission | **PROHIBITED** without written permission | **PROHIBITED** | Yes, sufficient to block. Cure: NSE written permission or an NSE Data licence. |
| **NSE downloadable files** (`nse_index_close_archive`, `nse_index_constituents`, `nse_fo_secban`) | **PROHIBITED** when downloaded by a script (automated collection). A *manual* download is "available for download", which lifts only the "aggregate, copy or duplicate" sentence; the storing/distributing sentence still requires written permission | **AMBIGUOUS** for a manual download; **PROHIBITED** for an automated one | **PROHIBITED** without written permission | **PROHIBITED** without written permission | **PROHIBITED** without written permission | **PROHIBITED** | Sufficient to block automated use; manual-download private use remains ambiguous. Index data may also be NSE Indices Ltd.'s (not reviewed). |
| **DMB derived / Market Structure** | Inherits: **PROHIBITED** | Inherits | Inherits | Inherits | Inherits | Inherits | Derivation does not launder rights. |
| **Zerodha Kite Connect** (Kite Terminal's own source; not a DMB source) | n/a | **AMBIGUOUS**: §4b forbids databases / "permanent copies … with the intent of redistributing"; whether the qualifier covers the whole list (Kite keeps `ohlc_daily` candles and quotes locally, never redistributed) is unclear | n/a (no Kite data flows to DMB) | **Permitted in principle for the account holder's own use**: the API is licensed to the developer for use within India; public display of live data is prohibited | **PROHIBITED** for live market data (§2a); historical display not addressed | **PROHIBITED** (§4b) | Sufficient for private, single-user display to the account holder. Local permanent storage: ask Zerodha (§3). |
| **Synthetic fixture** | PERMITTED | PERMITTED | PERMITTED (test profile only) | PERMITTED (test profile only) | n/a | n/a | Yes: no third-party content. |

### 5.3 Consequences

1. **DMB → Kite real-data handoff: remains NO-GO.** Nothing permits Category 3 for any real source; Yahoo and NSE expressly prohibit it without permission. No real bundle is exported (`RIGHTS_BLOCKED`) and Kite would refuse one (`RIGHTS_NOT_APPROVED`).
2. **DMB private real-data research: NO-GO on documented rights.** The 2026-11-03 record called Category 1 "unresolved". The terms now on file prohibit the way DMB collects (automated, via `yfinance` and scripted NSE requests) without the provider's express or written permission. The existing owner decision to run the private desk does not change what the terms say. This record does not switch anything off. Running DMB's real-data pipelines is the owner's decision and runs under DMB's own runbooks, but it is not supported by documented rights.
3. **Public and commercial use: remain BLOCKED.**
4. **Kite Terminal without DMB data.** Kite Terminal can operate on its own directly authorized source: Kite Connect data shown to the account holder in their personal, non-public app. It needs no DMB data. The one open question is local permanent storage of Kite Connect candles and quotes (§3, Zerodha row).
5. **What would change the status:** for Yahoo, express prior written permission or replacing it with a licensed feed. For NSE, written permission or an NSE Data and Analytics licence for non-commercial use (and NSE Indices Ltd. for index data). Each must name the use (private research, local storage, cross-app transfer, public display). Then record it in DMB `rights.py` one source at a time, with a reviewed commit (§3).
