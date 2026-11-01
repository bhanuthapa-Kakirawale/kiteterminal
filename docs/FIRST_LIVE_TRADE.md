# First Live Trade — Protocol

This is the manual procedure for the **first real (LIVE) order** placed through
Kite Terminal, after LIVE-0. Read it end to end before starting.

LIVE-0 did **not** place this order and nothing in the terminal places it for
you. Every step below that changes something is yours to do deliberately.

> **Live Readiness is operational readiness only.** READY means every known
> technical precondition passed. It does not assess whether the trade is a
> good one.

---

## Before the day

1. **Kite developer console → your Kite Connect app → static IP.** Confirm
   which IP is registered. Earlier live attempts from this terminal were
   rejected because no static IP was configured. Kite rejects every API order
   from any other IP.
2. **Decide your first-live limits** (write them down, you will type them in):
   - max order notional (₹) — the most one order may be worth;
   - daily loss limit (₹) — realized live loss at which new entries stop;
   - max new live entries per day — **1** for the first trade.
3. **Choose one liquid NSE stock** you understand, for a **small BUY + CNC**
   (delivery) position whose value is well inside the notional cap.

## The protocol

1. **Start the application** with the launcher, which enforces a single
   worker and no auto-reload:
   ```
   .venv\Scripts\activate
   python -m backend.server
   ```
   Open `http://127.0.0.1:8000` and **log in to Kite** (required every
   trading day).

2. **Review System Diagnostics** (sidebar → Diagnostics). Schema current,
   integrity ok, workers running, the Kite session VALID, no flagged history
   on the symbol you intend to trade.

3. **Run Live Readiness** (sidebar → Live Readiness). Click **Run broker check
   (read-only)**. It reads your profile, holdings, positions, margins, orders
   and GTTs once. It never places, modifies or cancels anything.

4. **Resolve every blocking item until only the trading lock remains.**
   - *Settings* → **Fill FIRST LIVE / CAUTIOUS profile…** → type your own
     **Max Order Value (₹)** and **Max Daily Loss (₹)** → review every other
     field (max open positions, stock/sector exposure %, entry price
     deviation %) → **Save**. The profile fills the form only; it never saves
     and never picks a rupee amount.
   - *Live Readiness → Static IP*: enter the IP from the developer console,
     tick the confirmation, **Confirm static IP**. Optionally click **Check
     this machine's outbound IP** (one request to api.ipify.org): VERIFIED if
     it matches, FAILED if it does not — a FAILED check blocks live entries.
   - Warnings (research data, ticker, non-critical findings) never block, but
     read them.

5. **Confirm READY — except the trading lock.** The verdict reads *NOT READY —
   trading lock is ON / every other check passed*. Do not continue on any
   other NOT READY reason.

6. **Verify the Kite static IP manually once more** in the developer console,
   especially if this machine or its network changed since you confirmed it.

7. **Confirm the global mode is LIVE.** Turn on the **Live trading** toggle in
   the mode banner → read and accept the dialog (it repeats the static-IP
   requirement). The terminal never switches this for you. Switching mode does
   not unlock trading.

8. **Keep the trading lock ON** while you prepare the ticket.

9. **Create one small BUY + CNC entry** (New Ticket, LIMIT order recommended).

10. **Inspect the ticket** in Approvals / the confirm dialog:
    quantity · entry (limit) price · stop · target · mode **LIVE** · account ·
    notional (quantity × price, within your cap) · risk checks all PASS.

11. **Unlock trading explicitly** (the lock button in the account-safety strip
    under the mode banner, with a reason). Re-open Live Readiness: it must now read **READY** and show the
    **First live order — manual checklist**. Tick each item yourself:
    - [ ] Static IP verified in the Kite developer app
    - [ ] Correct trading account confirmed
    - [ ] Max order notional reviewed
    - [ ] Daily loss limit reviewed
    - [ ] Trading lock intentionally disabled
    - [ ] Order quantity reviewed
    - [ ] BUY + CNC confirmed
    - [ ] Stop and target reviewed
    - [ ] No unresolved broker order
    - [ ] No critical reconciliation warning

12. **Approve that ONE ticket.** Outside market hours you must tick "Send as
    AMO" explicitly; otherwise it is refused.

13. **Observe immediately:**
    - the ticket's state and its **broker order id** (not `PAPER-…`);
    - *Orders & Trades*: the order in Kite's order book with the terminal's tag;
    - *Positions*: ENTRY WORKING → OPEN once filled;
    - the order monitor picks the fill up within ~20 s.

14. **When filled, verify:**
    - filled quantity equals the ticket quantity (or see *PARTIAL* below);
    - a **GTT id** is shown and the position reads **PROTECTED**, with the GTT
      quantity equal to the filled quantity;
    - the position state is OPEN.

15. **Cross-check in the official Kite web/app:** the order (status COMPLETE,
    quantity, average price), the holding/position, and the GTT (both legs:
    stop and target, correct quantity).

16. **Re-lock trading** once the entry is filled and protected, and do **not**
    place a second live trade until this first one has completed its whole
    lifecycle (exit filled, position CLOSED, one LIVE journal row, no
    reconciliation finding) and you have compared it with Kite.

If **anything** differs from what you expect at any step: **lock trading
first**, then investigate. Exits stay possible while locked.

---

## If something goes wrong

The same rules apply to every case:

- **Lock trading first.** The lock blocks only new entries; exits, protection
  upkeep, order monitoring and reconciliation keep running.
- **Look at Kite itself** (web/app order book, positions, GTTs) — it is the
  source of truth for what exists at the broker.
- **Use the terminal's own views:** Positions (state, protection, reconciliation
  issues), Orders & Trades, Trade Queue → reconciliation findings, Diagnostics.
- **Never resubmit an order whose outcome is UNKNOWN.** A duplicate real order
  is worse than a delayed one.
- **Never edit the database by hand.** Every state the terminal can be in has
  a supported path; if one does not seem to, stop and investigate.

| Situation | What the terminal does | What you do |
|---|---|---|
| **REJECTED** by the broker (ticket FAILED) | Nothing reached the exchange. The rejection reason from Kite is in the ticket's events. The daily entry count does not include it. | Read the reason. `IP not allowed` / similar → static IP (Live Readiness → Static IP; developer console). Margin/RMS → funds. Fix the cause before creating a **new** ticket; the failed one is final. |
| **UNKNOWN** submission (network error mid-send) | Shows **SUBMISSION UNKNOWN**. The order monitor searches Kite's order book for the ticket's tag for 15 minutes and marks it adopted, not found, or NEEDS REVIEW. Every further **live entry is refused** (Live Readiness `UNKNOWN_LIVE_ORDERS`, confirm-time `LIVE_PREREQUISITE_MISSING`) until it is resolved. | Lock trading. **Do not resubmit.** Look in Kite's order book for an order carrying the ticket's tag. Wait for the monitor's verdict; if NEEDS REVIEW, compare with Kite and act there. |
| **PARTIAL** fill (ENTRY PARTIAL) | The protective GTT is sized to the **filled** quantity and kept in step as more fills arrive (PK-13). The rest of the order keeps working. | Decide: let it keep working, or cancel the remainder (Positions → Cancel order, or Orders & Trades). Do not place a second order for the missing shares. |
| **Order OPEN for too long** (limit not reached) | The order stays working; the position reads ENTRY WORKING. A stale strategy signal is never re-priced. | Cancel it from Positions/Orders & Trades if the setup no longer holds. Do not "chase" by placing a new order while the first is still working. |
| **GTT missing** (UNPROTECTED / protection failed / `GTT_MISSING`, `PROTECTION_MISSING`) | Reconciliation flags it; the position shows the protection problem. | Lock trading. Check Kite's GTT list. If there is truly no stop, place a stop-loss **in Kite** immediately (the existing guidance for "protection failed"), then mark the finding reviewed once it is covered. |
| **Quantity mismatch** (`GTT_QUANTITY_*`, `BROKER_POSITION_SHORTFALL`) | Reconciliation records the finding; shortfall is **critical** and blocks new live entries until reviewed — re-checked at every confirmation since SEC-1 (`RECONCILIATION_CRITICAL`), not only on the Live Readiness page. Exits and Emergency Exit are never blocked; Emergency Exit names the affected positions. | Lock trading. Compare Kite holdings/positions/GTT quantities with the Positions view. Correct at the broker as needed, then mark reviewed — a **reason is required** and the review is logged (`reconciliation_reviewed`). |
| **Stale or incomplete reconciliation / stalled worker** (after a restart, a broker outage, a cycle whose checks failed) | Every live new entry is refused at confirmation (`LIVE_STATE_UNVERIFIED`, 503) until reconciliation has compared the app with Kite completely and recently and the order monitor, trailing and reconciliation loops are healthy (SEC-1). Exits are not affected. | Wait for the next reconciliation cycle (Diagnostics). If it keeps failing, find out why before trading. |
| **Possible double sell** (`POSSIBLE_DOUBLE_SELL`, `ORPHANED_GTT_AFTER_EXIT`, `GTT_ON_CLOSED_POSITION`) | Critical finding; new live entries are blocked until it is reviewed. | Lock trading. In Kite: check for a short position and delete any GTT on shares you no longer hold. Only then mark the finding reviewed. |
| **Circuit breaker OPEN** | New live entries blocked after repeated broker/transport failures. | Find the cause (Diagnostics, logs, Kite status). Reset the breaker manually only once it is understood. |
| Anything you cannot explain | — | **Emergency Exit All** (type `EXIT ALL`) exists for the case where every open position must go; otherwise lock trading and investigate before acting. |

---

## After the first round trip

- The position reads CLOSED only after Kite proves the exit filled; exactly one
  LIVE row appears in the Journal.
- Compare realized P&L with Kite's contract note (the journal is gross of some
  charges).
- Only after this comparison consider raising the cautious limits — one at a
  time, deliberately, in Settings.
