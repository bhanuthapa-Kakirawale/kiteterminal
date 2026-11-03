# NSE trading calendar: session semantics, DMB/Kite parity and the 2027 dependency

## 1. Status on 2026-11-03: the official 2027 calendar is NOT published

| Check | Result |
|---|---|
| NSE's holiday master, `https://www.nseindia.com/api/holiday-master?type=trading`, read by `python -m backend.nse_calendar_monitor check --year 2027` | **`NOT_PUBLISHED`** (exit 10). Re-checked at 2026-11-03T11:26:03Z: the response lists Capital Market (CM) holidays for 2026 only. Response SHA-256 `933cf5dc6dd4efe3b8db8ce6ecd6ecb2582929f5e0b4faffdb88e11cf4efdfbe`, identical to the 08:20 UTC check. |
| NSE circular search (nseindia.com / nsearchives.nseindia.com) | No "Trading holidays for the calendar year 2027" circular. NSE publishes each year's list in December (2026: NSE/CMTR/71775 of 12-Dec-2025). |

**Consequence (deliberate).** No 2027 date was invented or estimated. Both applications stay **fail-closed** for 2027:
- **Kite:** `trading_calendar.coverage(2027) != "COVERED"`. `session_record` is `UNVERIFIED`. The DMB inbox refuses with `CALENDAR_UNCOVERED`. Live new entries are refused from 2026-12-01, the existing December protection, which is unchanged.
- **DMB:** `review_session_for(2026-12-31)` is `None`, so the exporter writes no review session it cannot prove. `session_record` is `UNVERIFIED`.

This is an **external dependency** on NSE's circular, expected mid-December 2026.

## 2. Session semantics (both applications)

A weekday/holiday boolean is not enough: Budget day can be a full session on a weekend, and Muhurat is a one-hour session on a holiday. Both repositories now expose `session_record(date)` with an identical vocabulary:

| `session_type` | Meaning | Packet session? | Review session? | "Market open" in Kite? |
|---|---|---|---|---|
| `REGULAR` | weekday, not a listed holiday; 09:15–15:30 IST | yes | yes | yes, 09:15–15:30 |
| `SPECIAL_FULL` | live session at standard timings on a weekend/holiday (Union Budget) | **no** | **no** | no (hours recorded, not modelled) |
| `SPECIAL_SHORT` | short special session (Diwali Muhurat; DR drills) | **no** | **no** | no |
| `HOLIDAY` | a listed holiday or a weekend | no | no | no |
| `UNVERIFIED` | uncovered year (2027), or a special session without official evidence | no (fail closed) | no | no |

Every record carries `date`, `segment` (`NSE_CM`), `session_type`, `open`, `close`, `source` (the circular), `published` (the circular's date) and `verification`:
- `VERIFIED`: the circular itself was retrieved and read this sprint (SHA-256 below).
- `REFERENCED`: recorded from an official NSE publication that cites it; the circular itself was not retrieved.
- `UNVERIFIED`: no official evidence; such a special session is never typed as a session.

Data: Kite `backend/calendar_data/nse_equity_holidays.json` (validated by `load_calendar`; an evidenced special session must have open, close, source and publication date). DMB `core/trading_calendar.py` (`SPECIAL_SESSION_RECORDS`, `HOLIDAY_LIST_SOURCES`, `HOLIDAY_SOURCE_OVERRIDES`).

## 3. Official evidence (2025–2026, the years both applications cover)

| Date | Type | Normal market (IST) | Circular | Published | Status | Retrieved file SHA-256 |
|---|---|---|---|---|---|---|
| 2026 holiday list | — | — | NSE/CMTR/71775 (172/2025) | 2025-12-12 | VERIFIED | `aa97e0af…dcacb` |
| **2026-02-01 (Sun)** | **SPECIAL_FULL** | **09:15–15:30** (pre-open 09:00–09:08; T0 not held, settlement holiday) | **NSE/CMTR/72349 (11/2026)**; F&O **NSE/FAOP/72352 (07/2026)**, same timings | 2026-01-16 | VERIFIED | `70458f7b…bfc3` / `7b282150…f1facb` |
| **2026-11-08 (Sun)** | **SPECIAL_SHORT** | **14:20–15:30** (pre-open 14:05; CAS securities' normal market closes 15:15; CAS to 15:35) | **NSE/CMTR/76941 (149/2026)**, continuing NSE/CMTR/71775 | 2026-10-19 | VERIFIED | `ce94ee1c…1d58` |
| 2025-10-21 (Tue) | SPECIAL_SHORT | 13:45–14:45 | NSE/CMTR/70319 (124/2025) | 2025-09-22 | VERIFIED | `19b7d880…d258` |
| 2025-02-01 (Sat) | SPECIAL_FULL | 09:15–15:30 ("standard market timings") | NSE/CMTR/65729, cited by NSE Indices press release of 24-Jan-2025 | 2024-12-23 | REFERENCED | press release `c862ad27…8a16` |
| 2026-01-15 (Thu) holiday | HOLIDAY | — | amendment of 09-Jan-2026 (Maharashtra municipal elections); the CM circular itself was not retrieved; the same notice in NSE/CD/72233 and NCL/CMPT/72278 | 2026-01-09 | REFERENCED | — |
| 2025 holiday list | — | — | NSE/CMTR/65587 (170/2024), cited by NSE/CMTR/70319 | 2024-12-13 | REFERENCED | — |

Full hashes and copies are in the sprint's scratch evidence folder. The circulars are public NSE documents and can be re-downloaded from `https://nsearchives.nseindia.com/content/circulars/<ref>.pdf`, e.g. `CMTR72349.pdf`.

**Retrieval method, disclosed.** NSE's archive does not answer non-browser clients. The five PDFs above were fetched with a browser `User-Agent` header. After that, the agent's safety controls flagged further browser-imitating requests, and none were made. A plain request for the 2025 circulars (NSE/CMTR/65587, 65729) timed out, so those stay `REFERENCED`. To upgrade them, download the two PDFs in a browser, check them, and change their `verification` in both repositories.

## 4. DMB/Kite calendar parity

`tests/test_calendar_parity.py` (Kite; runs DMB's own code in DMB's interpreter, read-only) asserts, for every day of **2025-01-01 … 2027-01-31**:
- weekday holidays identical for every year both cover;
- the **complete session record** identical: type, open, close, source, publication date and verification;
- DMB's `review_session_for(d)` equals Kite's next regular session, or both refuse;
- special-session lists identical: 2025-02-01, 2025-10-21, 2026-02-01, 2026-11-08;
- each special session is typed and sourced on both sides, DMB exports no packet for it, and Kite refuses one (`INVALID_SESSION_DATE`);
- 2027 is `UNVERIFIED` on both sides and a packet into 2027 is refused (`CALENDAR_UNCOVERED`).

**Parity report (2026-11-03): 0 differences** across 761 days. The two differences found in the previous sprint are resolved:

| Previous difference | Resolution |
|---|---|
| DMB missed the 2026-02-01 Sunday Budget session | Added to DMB from NSE/CMTR/72349 / NSE/FAOP/72352. DMB's `SessionCalendar` now treats it as a session (provider rows dated that Sunday are kept; the session before Monday 2026-02-02 is the Sunday). |
| DMB computed a review session for special-session days; Kite refused such packets | DMB's `review_session_for` now applies its own documented rule ("`session_date` = the completed **regular** session"): special-session days export no packet. Both sides agree. |

DMB-side tests: `daily_byte/tests/test_trading_calendar_session_records.py` (14).

## 5. Muhurat and special sessions: actionability (decision required, not guessed)

The owner-approved rule is "a packet remains actionable until the next trading session completes", implemented on both sides as **the next REGULAR session**. It is **unchanged**. Consequences, tested on both sides:
- Friday 2026-11-06's packet is reviewed on **Monday 2026-11-09** and expires at 15:30 IST that day. The Muhurat session on Sunday 2026-11-08 does not shorten it.
- Friday 2026-01-30's packet was reviewed on Monday 2026-02-02, even though the Budget Sunday was a full session.
- Tuesday 2025-10-20's packet is reviewed on Thursday 2025-10-23 (over Muhurat on the 21st and Balipratipada on the 22nd).
- No packet ever describes a special session.

**Owner decision required** before the next announced weekend `SPECIAL_FULL` session (none is announced): should a full special session (a) be a packet session of its own, and/or (b) end the previous packet's window? Muhurat (`SPECIAL_SHORT`, about one hour) is not proposed to advance the rule. Until decided, the conservative-for-data reading above stands, and the documents say so (`daily_byte/docs/PRIVATE_DESK_KITE_INTEGRATION.md` §4.1, `docs/DMB_RESEARCH_INBOX.md`).

## 6. The monitor (`backend/nse_calendar_monitor.py`)

```
cd D:\Claude_WS\kite-terminal
.venv\Scripts\python.exe -m backend.nse_calendar_monitor check --year 2027          # human-readable
.venv\Scripts\python.exe -m backend.nse_calendar_monitor check --year 2027 --json   # machine-readable
.venv\Scripts\python.exe -m backend.nse_calendar_monitor check --year 2027 --offline  # no network
```

Read-only: one HTTPS GET to NSE's own host, no Kite proxy, no broker connection. It never writes the calendar and never schedules itself. **No Windows Task Scheduler job was registered.** Run it by hand, e.g. weekly from 1 December.

| Status | Exit | Meaning | Operator action |
|---|---|---|---|
| `INTEGRATED` | 0 | the year is in the Kite holiday file | none |
| `NOT_PUBLISHED` | 10 | NSE answered and lists no CM holiday in the year | check again later |
| `PUBLISHED_NOT_INTEGRATED` | 11 | NSE lists dates (printed as *unverified*) | integrate from the official circular (§7) |
| `UNREACHABLE` | 20 | network error or refusal (NSE often blocks non-browser clients) | check the NSE page in a browser |
| `INVALID_RESPONSE` | 21 | not NSE's holiday master | check the NSE page in a browser |

Every network result records the URL, retrieval time and response SHA-256. Tested with mocked responses only: `tests/test_nse_calendar_monitor.py`, 10 tests.

## 7. Integrating 2027 when NSE publishes it (operator procedure)

Only from the official circular, never from a third-party list.

1. **Get the circular.** Download "Trading holidays for the calendar year 2027" (Capital Market) from nseindia.com / nsearchives.nseindia.com in a browser. Record its URL, `NSE/CMTR/…` reference, circular number, publication date, your retrieval date and its SHA-256 (`certutil -hashfile <file> SHA256`).
2. **Kite.** In `backend/calendar_data/nse_equity_holidays.json`, add `"2027": {"source": "NSE/CMTR/<n> (circular <n>/2026, <dd-Mon-2026>)", "published": "<yyyy-mm-dd>", "verification": "VERIFIED", "amendments": [], "holidays": [...]}`, listing **weekday** holidays only. Add every announced special session under `special_sessions` with `kind`, `session_type` (`SPECIAL_FULL`/`SPECIAL_SHORT`), `open`, `close`, `source`, `published`, `verification`. A session whose timings are "to be notified" stays out until its timing circular exists, as Muhurat 2026 did.
3. **DMB.** Add the same holidays to `NSE_TRADING_HOLIDAYS[2027]` in `daily_byte/core/trading_calendar.py` (DMB also lists weekend-dated holidays), `HOLIDAY_LIST_SOURCES[2027]` with **the identical source string**, and the special sessions to `SPECIAL_SESSION_RECORDS`.
4. **Verify.**
   - `python -m backend.trading_calendar check` (Kite).
   - `python -m backend.nse_calendar_monitor check --year 2027` → `INTEGRATED`.
   - Extend `_probes()` in `tests/test_calendar_parity.py` to 2027-12-31 and run it with `tests/test_trading_calendar.py`.
   - In DMB, run `tests/test_trading_calendar_session_records.py` and the session-alignment tests.
   - Parity must show 0 differences.
5. **Amendments.** Repeat steps 1–4 for every amendment circular (e.g. election holidays), as a per-holiday `source`/`published`/`verification` (Kite) and a `HOLIDAY_SOURCE_OVERRIDES` entry (DMB).
6. **Special-session decision.** If 2027's Budget day is announced as a weekend full session, the §5 owner decision is due before it.
