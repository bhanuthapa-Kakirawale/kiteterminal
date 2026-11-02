# SEC-1 final safety sprint (2026-11-02)

Local commits on `feature/signal-detail-and-strategy-revision` after
`abbf35e`. Nothing was pushed. FakeKite and temporary or scratch databases
only. No PK-25 frozen file was changed.

| Workstream | Result | Commit | Details |
|---|---|---|---|
| P1-D review-token uniqueness | DONE | `7fcd2aa` | Migration v8 `review_generation`, a 128-bit nonce for every review, bound into the token. Re-review's compare-and-set includes it. See `SEC1_P1D_REPORT.md`. |
| Browser validation | DONE | `ab8aafb` | Demo `--review-controls` (server clock, simulated Kite login, tamper, loss alert); `tests/e2e_p1d.py`; `e2e_ui.py --pin-clock`. |
| P1-F per-launch credential | DONE | `908b0c8` | See `SEC1_P1F_REPORT.md`. |
| P1-E acceptance vs fills | DONE | `bf50966` | See `SEC1_P1E_REPORT.md`. |
| NSE 2027 calendar | **BLOCKED** | — | NSE has not published the 2027 circular. The calendar stays fail-closed; see `DEPLOYMENT.md`, "NSE holiday calendar". |

## Final verification

- **Full suite:** 2199 passed, 0 failed, 0 skipped. The baseline was 2029;
  the additions are:
  - P1-D generation tests (+6);
  - P1-F credential tests (+142);
  - P1-E progress tests (+22).
- **`tests/e2e_p1d.py`:** 13/13 checks.
  - The 12 P1-D checks from the brief, plus the P1-F check.
  - Every expected 401/409 is declared and observed with its code.
  - No other console, page or HTTP error.
- **`tests/e2e_ui.py --pin-clock`:** 53/54. The one failure, "risk-blocked
  signal listed separately", is pre-existing demo data (PK-19 report). Before
  this sprint the run stopped at its first approval on a weekend.
- **P1-E in the browser:** the ticket detail shows "Paper broker / fill:
  accepted · awaiting fill · 0/4 · order working".
- **Mutation checks**, each caught:
  - P1-D: the generation dropped from the token or from the compare-and-set;
  - P1-F: the guard unregistered (126 of 142 tests fail);
  - P1-E: an acknowledgement reported as `FILLED`.
- **Frozen files:** `test_26` / `test_36` pass, and
  `git diff abbf35e --name-only` lists no frozen file.

## Remaining limitations

- **Live entries from 2026-12-01.** The 2027 calendar is blocked, so every
  live new entry is refused from that date until NSE's circular is added.
- **P1-F does not stop same-user malware** (browser cookie store, `.env`, DB),
  browser extensions with host access, or debugger/admin access. Reads stay
  Host-checked only. Login CSRF (P2-G) is open.
- **Restart re-login.** After every app restart or logout, a Zerodha login
  is needed before any change, Emergency Exit included. Kite web and the
  GTTs are the fallback meanwhile.
- **P1-E** leaves the stored `status` vocabulary unchanged. The positions list
  carries `entry_progress`, but does not render it yet.
- **The final gate** checks the claimed row and does not re-read it. An
  out-of-band database write after the claim cannot change what is sent, but
  is not itself flagged (`SEC1_P1D_REPORT.md`).
- **Demo-only routes.** `/demo/authorize` and `/demo/kite-login` hand out the
  credential freely. They exist only in `tests/demo_server.py`.
- **The demo used to load the real `.env`'s `KITE_REST_PROXY`.** This was
  pre-existing. The demo now pins it empty.
