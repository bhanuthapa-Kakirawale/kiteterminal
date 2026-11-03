# DMB → Kite isolated synthetic acceptance test

This test runs today, on any date, without the real account database, a broker, real credentials or real DMB data. It uses:

- **DMB's real exporter** on the committed synthetic fixture `daily_byte/tests/fixtures/private_desk_handoff/synthetic_handoff_2026-11-02.json` (SHA-256 `c6361bcb…a292776`);
- **Kite's real validator, inbox, ticket path and P1-D review**;
- **a scratch database** and **FakeKite**;
- **the synthetic test profile.** In this profile, synthetic bundles are actionable and **LIVE cannot be armed**.

Nothing in it asks you to enter real trading values or to place a real order.

## Requirements

| Item | Expected location |
|---|---|
| Kite checkout and venv | `D:\Claude_WS\kite-terminal\.venv` |
| DMB checkout and venv | `D:\Claude_WS\daily_byte\venv`, as a sibling folder. Or set `DMB_REPO` to its path. |
| Microsoft Edge | only for part C, the browser check |

**Environment variables.**

- **None are required.** The runner sets everything it needs for its own child processes.
- **It removes every `KITE_*` variable** from those children, so a real `.env` cannot leak in.
- **For DMB**, it sets `DAILY_BYTE_OUT=<workdir>\dmb_out`, so DMB's real output root is never used.
- **Optional:**
  - `DMB_REPO`, if DMB is not the sibling folder;
  - `--workdir`, if you don't want the default `%TEMP%\dmb_kite_acceptance`;
  - `--port`, if you don't want the default 8790.

## A. Automated end-to-end run (one command)

```
cd D:\Claude_WS\kite-terminal
.venv\Scripts\python.exe tests\acceptance_dmb_kite.py
```

**Expected:** 42 `[PASS]` lines, then `42/42 checks passed` and `LOCAL SYNTHETIC INTEGRATION ACCEPTANCE: PASS`. The exit code is 0, and the results are also written to `<workdir>\acceptance_result.json`. It takes about a minute.

The runner manages its own processes:

1. **Start:** it starts the isolated Kite test server: `tests\demo_server.py --dmb-synthetic-profile`, on `http://127.0.0.1:8790`.
2. **Restart:** it stops that server and starts it again on the same scratch database (`--keep-db`).
3. **Stop:** it stops the server at the end, or on Ctrl+C.

`--keep-running` leaves the restarted server up afterwards.

| Stage | What is checked | Expected result |
|---|---|---|
| 1 | Environment; fixture hash; writes before the test login | Venvs present; fixture SHA-256 matches; 401 `TERMINAL_SESSION_REQUIRED` |
| 2 | DMB export into the scratch handoff folder | `CREATED`, `data_origin` SYNTHETIC |
| 3 | Bundle id, packet ids, file SHA-256 | All recomputed and equal |
| 4 | Import | Listed, never auto-imported; `IMPORTED` with the same file hash |
| 5 | Provenance and actionability | DMB producer, artifact ids, PERMITTED *synthetic* rights, review session 2026-11-03, profile `SYNTHETIC_TEST`. INFY actionable; TCS research only (`QUALITY_REPLAY_NOT_MATCHED`) |
| 6 | Duplicate import | `DUPLICATE`, still 2 items |
| 7–9 | Note, dismiss, research watchlist | Recorded |
| 10 | Strategy scan universe | Unchanged |
| 11–12 | Start ticket during the review session | Prefill is exactly symbol, exchange, rationale; rationale starts `[SYNTHETIC TEST PACKET]` |
| 13 | Manual input required | A ticket without side, quantity or prices is refused (422). The synthetic values are typed in by the runner and the ticket awaits approval. On the **scratch instance only**, the sector-exposure limit is raised, because the demo portfolio is about 64% IT by design and both fixture symbols are IT. |
| 14 | P1-D | Review stamped and current. Approval without the shown token is refused (`TICKET_REVIEW_CHANGED`). The ticket is linked. LIVE arming is refused (`SYNTHETIC_TEST_PROFILE`). Approval with the token runs in **dry run** on the paper broker. |
| 15 | Positions fill status | `ACCEPTED / AWAITING_FILL 0` → `PARTIAL 1/2` → `FILLED 2/2` with an average price |
| 16 | Post-claim tampering | `TICKET_TAMPERED_AFTER_CLAIM`, nothing sent; audit = snapshot + `TAMPER_DETECTED(quantity)`; chain verifies; revert refused |
| 17–18 | Restart on the same database | `terminal_authorized=false` and account reads refused (401, P2-G R-F2 - before P2-G this check expected reads to work); the old credential is refused for writes (401); a fresh login issues a new cookie and a new terminal token, the old token is dead, and the current cookie *without* the token reads and writes nothing (P2-G R-F3) |
| 19 | Emergency Exit (FakeKite) | Before re-login: 401, no broker write. After a fresh login: an exit submitted for every live demo position. |
| 20 | Nothing real touched | DMB's handoff files are byte-identical; the server ran on the scratch database with the profile active; the real `kite_terminal.db*` files are unchanged in size and mtime (only `stat()`-ed, never opened) |

## B. Manual walk-through in the UI (same isolated environment)

```
cd D:\Claude_WS\kite-terminal
.venv\Scripts\python.exe tests\acceptance_dmb_kite.py --serve-only
```

This exports the fixture and starts the isolated instance on `http://localhost:8790`, with the server clock pinned to 2026-11-03 11:00 IST, during the review session. It stays up until Ctrl+C.

1. **Open a separate browser profile**, for example an Edge InPrivate window, so the test cookie never sits next to the real terminal's.
   - The test profile uses its own cookie name (`kt_test_session`).
   - Cookies are not port-scoped (P2-G R-F3), so keeping the two profiles apart is still the safe habit.
2. **Go to `http://localhost:8790/demo/authorize`.** This is the test login. It opens the terminal.
3. **Open DMB Research Inbox.** You should see the disclaimer and one bundle (session 2026-11-02).
   - **Import.** You should see INFY ("can start a ticket") and TCS ("research only"), both labelled SYNTHETIC.
   - **Import again.** You should still see 2 items.
4. **Open INFY.** Check the packet id, source session, evidence time, review session 2026-11-03, the evidence direction marked "not a trade side", replay MATCHED, and Private handoff PERMITTED.
5. **Add a note.** Then add INFY to the research watchlist. Open TCS and **Dismiss** it (confirm the dialog).
6. **Start ticket on INFY.** Only the symbol (INFY), the exchange (NSE) and the rationale are filled. Direction, order type, product, quantity, entry, stop and target are empty, and the form cannot be submitted.
7. **Enter synthetic values:** BUY, LIMIT, CNC, quantity 2, entry 1180, stop 1150, target 1250. Then Create.
   - The approval dialog opens with "reviewed … — current". Approve: it runs in **DRY RUN** on the paper broker.
   - The mode switch to LIVE is refused in this profile.
8. **Positions.** The new row's **Broker / fill** column shows "Broker: accepted" and "Fill: accepted, not filled · 0/2".
9. **Stop with Ctrl+C.**

Part A checks restart and Emergency Exit behaviour automatically, in stages 17–19, against a real process restart on the same scratch database. In manual mode the paper exchange advances only on demand, so the order stays "accepted, not filled". Part A drives it through partial and full fills.

## C. Browser check (headless Edge, 31 checks)

```
.venv\Scripts\python.exe tests\e2e_dmb_inbox.py
```

**Expected:** `31/31 browser checks passed`. Screenshots are written to `%TEMP%\dmb_kite_browser\shots`.

## Reset the scratch environment

```
.venv\Scripts\python.exe tests\acceptance_dmb_kite.py --reset
```

- **What it deletes:** only a folder carrying the `.dmb_kite_acceptance` marker. Every run also recreates it.
- **What it refuses:** a workdir inside `kite-terminal`, `daily_byte` or the real database folder.
- **What it never touches:** the real database, DMB's `output\` and DMB's fixtures.

## Test failure versus an intentionally blocked operation

| You see | Meaning |
|---|---|
| `[FAIL]` in the runner, or a non-zero exit | **A test failure.** Read the detail printed after `--`. |
| `SYNTHETIC_NOT_PERMITTED` on import in the **real** terminal | **Intended.** Production refuses synthetic bundles. |
| `RIGHTS_NOT_APPROVED` / `RIGHTS_BLOCKED` | **Intended.** Real-data handoff is blocked pending rights confirmation. |
| `CALENDAR_UNCOVERED` for a 2027 date | **Intended.** NSE's 2027 calendar is not yet published (see `docs/NSE_CALENDAR_MONITORING.md`). |
| `SYNTHETIC_TEST_PROFILE` when switching to LIVE in the test instance | **Intended.** The test profile can never go live. |
| `TERMINAL_SESSION_REQUIRED` after a restart or logout | **Intended.** A fresh login is required before any write (P1-F). |
| `STALE_EVIDENCE` / `EXPIRED` | **Intended** if your own clock is past 2026-11-03 15:30 IST *and* you are not using the runner, which pins the clock. |
