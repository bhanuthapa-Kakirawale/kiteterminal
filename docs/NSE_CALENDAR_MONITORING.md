# NSE trading calendar: the 2027 dependency and how it is monitored

## Status on 2026-11-03: the official 2027 calendar is NOT published

| Check | Result |
|---|---|
| NSE's holiday master, `https://www.nseindia.com/api/holiday-master?type=trading`, read by `python -m backend.nse_calendar_monitor check --year 2027` | `NOT_PUBLISHED`. The response lists 20 Capital Market (CM) entries, all dated 2026: 15-Jan-2026 to 25-Dec-2026. Retrieved 2026-11-03 08:20:13 UTC. Response SHA-256 `933cf5dc6dd4efe3b8db8ce6ecd6ecb2582929f5e0b4faffdb88e11cf4efdfbe`. |
| NSE circular search (nseindia.com / nsearchives.nseindia.com) | No "Trading holidays for the calendar year 2027" circular found. The 2026 circular is NSE/CMTR/71775 (circular 172/2025, 12-Dec-2025); NSE publishes each year's list in December. |

**Consequence (unchanged, deliberate).** No 2027 date was invented or estimated. Both applications stay **fail-closed** for any packet or session that needs 2027:

- **Kite:** `trading_calendar.coverage(2027) != "COVERED"`, so the DMB inbox refuses with `CALENDAR_UNCOVERED`.
- **DMB:** `review_session_for(2026-12-31)` returns `None`, so the exporter writes no review session it cannot prove.

This is an **external dependency** on NSE publishing its circular, expected mid-December 2026.

## The monitor (`backend/nse_calendar_monitor.py`)

```
cd D:\Claude_WS\kite-terminal
.venv\Scripts\python.exe -m backend.nse_calendar_monitor check --year 2027          # human-readable
.venv\Scripts\python.exe -m backend.nse_calendar_monitor check --year 2027 --json   # machine-readable
.venv\Scripts\python.exe -m backend.nse_calendar_monitor check --year 2027 --offline  # no network
```

The monitor is **read-only**:

- It makes one HTTPS GET to NSE's own host and nothing else. It does not use the Kite REST proxy, the system proxy or any broker connection.
- It never writes the calendar.
- It does not schedule itself. No Windows Task Scheduler job was registered.

| Status | Exit | Meaning | Operator action |
|---|---|---|---|
| `INTEGRATED` | 0 | the year is in `backend/calendar_data/nse_equity_holidays.json` | none |
| `NOT_PUBLISHED` | 10 | NSE answered and lists no CM holiday in the year | check again later |
| `PUBLISHED_NOT_INTEGRATED` | 11 | NSE lists dates (printed as *unverified*) | integrate from the official circular (below) |
| `UNREACHABLE` | 20 | network error or refusal (NSE often blocks non-browser clients) | check the NSE page in a browser |
| `INVALID_RESPONSE` | 21 | NSE answered with something that is not its holiday master | check the NSE page in a browser |

Every network result records the URL, the retrieval time and the response SHA-256. It is tested with mocked responses only (`tests/test_nse_calendar_monitor.py`, 10 tests).

## Integrating 2027 when NSE publishes it (operator procedure)

Do this only from the official circular, never from a third-party list.

1. **Get the circular.**
   - Download "Trading holidays for the calendar year 2027" (Capital Market segment) from nseindia.com or nsearchives.nseindia.com.
   - Record its URL, circular reference (NSE/CMTR/…), circular number, publication date, your retrieval date and its SHA-256: `certutil -hashfile <file> SHA256`.
2. **Update Kite.** In `backend/calendar_data/nse_equity_holidays.json`:
   - Add `"2027": {"source": "NSE/CMTR/<n> (circular <n>/2026, <date>)", "amendments": [], "holidays": [...]}`.
   - List **weekday** holidays only.
   - Add any announced special session (Muhurat, Budget day) under `special_sessions`, with its `kind`.
3. **Update DMB.**
   - Add the same 2027 weekday holidays to `NSE_TRADING_HOLIDAYS` in `daily_byte/core/trading_calendar.py`.
   - Add the same special sessions to `SPECIAL_SESSIONS`.
   - Note the circular in the comment above the dict.
4. **Verify.**
   - `python -m backend.trading_calendar check` (Kite).
   - `python -m backend.nse_calendar_monitor check --year 2027` should now report `INTEGRATED`.
   - `pytest tests/test_calendar_parity.py tests/test_trading_calendar.py` (Kite). Then extend `KNOWN_*` in the parity test only for a deliberate, documented difference, and add 2027 boundary cases: the first and last session, every 2027 holiday, Muhurat.
   - In DMB, run its calendar tests.
5. **Amendments.** NSE amends the list during the year (election days, for example). Repeat steps 1–4 for every amendment circular, recording it under `amendments`.

## Calendar parity between DMB and Kite (`tests/test_calendar_parity.py`, 9 tests)

- **Regular sessions:** the weekday holidays for 2025 and 2026 are identical in both applications.
- **Review sessions:** for every day of 2025–2026, DMB's `review_session_for(d)` equals Kite's next regular session, or both refuse. The exceptions are the three special-session dates below.
- **2027:** both fail closed.
- **Muhurat:** 2026-11-08 (Sunday) is an exchange session but never a regular or review session. The packet of Friday 2026-11-06 is reviewed on Monday 2026-11-09 and expires at 15:30 IST that day. A packet claiming the Muhurat day as its review session is refused (`REVIEW_SESSION_MISMATCH`).
- **Holiday and weekend boundaries:**
  - 2026-11-09 → 2026-11-11 (Balipratipada, 2026-11-10);
  - 2026-12-24 → 2026-12-28;
  - 2026-10-19 → 2026-10-21 (Dussehra);
  - 2026-11-02 → 2026-11-03.

### Known differences, found by the parity test (reported, not changed in this sprint)

| Difference | Effect | Safe? |
|---|---|---|
| Kite lists **2026-02-01** (Sunday Union Budget live session) as a special session. DMB's `SPECIAL_SESSIONS` does not. | DMB's own session alignment treats that Sunday as a non-session. Review sessions are unaffected, because special sessions are never review sessions. | Yes for the handoff. Flagged for DMB's owner, since it concerns DMB's production data alignment, which this sprint does not change. |
| DMB treats its special sessions (2025-02-01, 2025-10-21, 2026-11-08) as sessions a packet may *describe*, and computes a review session for them. Kite refuses such a packet (`INVALID_SESSION_DATE`), because the agreed rule counts regular sessions only. | A DMB packet for a special-session day would be refused by Kite. | Yes. It fails closed on the Kite side. DMB's owner may want the exporter to skip special-session days. |
