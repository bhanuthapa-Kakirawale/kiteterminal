# Kite Terminal — User Guide

## 1. What This App Does

Kite Terminal watches a list of NSE stocks you choose (your holdings +
watchlist), looks for entry/exit signals from up to three built-in
strategies, and puts every proposed trade in front of you for approval
before anything is sent to your broker. It also lets you place manual trades
through the same approval-and-risk-check pipeline, manages protective
stop-loss orders automatically once a trade is live, and keeps a trading
journal of realized results.

**Nothing is ever traded without your confirmation**, and the app starts in
**Dry Run** mode by default, where "orders" are simulated and never reach
Zerodha.

## 2. First-Time Setup

1. **Get Kite Connect API credentials** from the Kite Connect developer
   console (developers.kite.trade) — you need an API key and API secret.
2. **Create a `.env` file** in the project root (copy `.env.example`) with:
   ```
   KITE_API_KEY=your_api_key
   KITE_API_SECRET=your_api_secret
   ```
   These are only read once, the first time the database is initialized, to
   create your account record. An encryption key
   (`KITE_TERMINAL_ENCRYPTION_KEY`) is generated automatically the first
   time you run the app and appended to `.env` — you don't need to set it
   yourself.
3. **Install dependencies**:
   ```
   pip install -r requirements.txt
   ```
4. **Start the server** from the project root:
   ```
   .venv\Scripts\activate
   python -m backend.server
   ```
   This binds to `127.0.0.1:8000` only, with auto-reload off and a single
   worker. `--port 8001` picks another port. Do **not** use `--reload` or
   `--workers` for real use: reload restarts the server whenever a file is
   saved, which can land between an order and its protective stop, and a
   second worker would run a second copy of every background loop. Reaching
   the terminal from another machine (e.g. a cloud VM) is covered in
   [DEPLOYMENT.md](DEPLOYMENT.md). It needs explicit settings, and the terminal
   must never be exposed to the public internet.
5. **Open the app** at `http://127.0.0.1:8000` or `http://localhost:8000`
   (with your port). Other addresses are refused with `INVALID_HOST` unless
   configured (see DEPLOYMENT.md).
6. **Log in**: click Login, which redirects to Zerodha's Kite login page.
   After you authenticate, Kite redirects back to the app and your session
   is active. **You must log in again every trading day** — Kite Connect
   requires a fresh daily login, this is a broker requirement, not an app
   limitation.

> **If you want to go live**, you must also register a static IP with your
> Kite Connect app in the developer console — the broker rejects API orders
> from unlisted IPs. This is called out in the app's own confirmation dialog
> when you try to switch out of Dry Run.

## 3. The Top Bar

At the top of every screen you'll see:

- **Market status** — whether NSE is currently open (09:15–15:30 IST,
  weekdays only).
- **Execution mode pill** — Dry Run or Live. Click it to switch (see §4).
- **Autopilot status pill** — links to the Approvals inbox; shows if
  automation is due, running, or waiting.
- **Trailing-engine heartbeat** — confirms the background stop-management
  loop is alive.
- **WebSocket connection dot** — green means you're receiving live price
  ticks and real-time updates without needing to refresh.
- **Theme toggle**, **refresh**, and **logout**.

A persistent banner also shows your current mode with two safety buttons
(see §9, Kill Switch).

## 4. Dry Run vs Live Mode

- **Dry Run (default)**: every trade is simulated. No order reaches
  Zerodha. This is the safe way to test strategies against real market
  data without risking money. Since PK-20 a dry-run approval places a
  **paper order** (`PAPER-O-…`) and a **paper protective GTT**
  (`PAPER-G-…`) at a simulated broker — it is *not* filled on approval.
  The paper order fills only when the observed market price allows (a
  LIMIT only at or better than its limit), slightly worse than that price
  (5 bps slippage), and the position then moves through the same states
  as a live one: ENTRY WORKING → OPEN → EXITING → CLOSED. The same
  market-hours rule applies (outside hours, tick "Send as AMO"). A working
  paper order can be cancelled from the Positions view. Paper P&L is gross
  (no brokerage or taxes).
- **Live**: real orders are placed through Kite Connect. Switching to Live
  requires you to explicitly confirm through a dialog (which also reminds
  you about the static-IP requirement). Switch back to Dry Run any time.

Your **Journal** (§8) tracks Dry Run and Live results completely
separately, so paper-trading history never mixes with real results.

## 5. Dashboard

Your home screen — a customizable grid of panels you can drag, resize,
hide, or add:

- **Portfolio Summary** — holdings value, day P&L, margins.
- **Holdings / Positions / Mutual Funds** — live from your Kite account.
- **Watchlist Summary** — your tracked symbols with live prices.
- **Today's Actions** — a quick preview of pending buy/exit candidates.
- **Alert banner** — flags large single-holding losses, over-concentration
  in one stock, large day P&L swings, an expiring session, or low margin
  (thresholds are visible in Settings-adjacent config).
- **Approvals callout** — a reminder if anything is waiting for your
  review.

## 6. Approvals — Your Trade Inbox

This is where every strategy-generated signal lands for your decision.
Sections:

- **Entry signals** — new trades a strategy wants to open.
- **Exit signals** — closes a strategy wants to make on positions it
  opened.
- **Stop adjustments** — proposed breakeven stop-tightening once a trade
  has moved far enough in your favor.
- **Blocked by risk** (collapsed by default) — signals that failed one or
  more risk checks and were auto-rejected; you can see exactly which check
  failed and why.
- **Autopilot run log** — history of automated scan runs.

Clicking **Approve** on a signal opens a confirmation dialog showing the
exact order that will be placed, the risk-check breakdown, and expected
capital/risk. Nothing executes until you click confirm in that dialog.

### Investigating a signal before you approve it

Every entry signal card carries two extra links:

- **Chart** — opens a full-screen **Signal Detail** view inside the app.
- **Kite** — opens that symbol's chart on kite.zerodha.com in a new tab.
  This needs an active Kite web session in the same browser, and it will
  **not** show your entry/stop/target lines — those exist only on the in-app
  chart. If the symbol couldn't be resolved to a Kite instrument the button
  is disabled and tells you why rather than opening a dead link.

The **Signal Detail** view exists to answer one question — *do I trust this
enough to click Approve?* — without leaving the app or taking a green
checkmark on faith. On the left is an annotated chart; on the right is the
evidence trail.

The chart shows the daily candles the signal was actually computed from
(read from the app's own stored history, never re-fetched, so what you see
is what the strategy saw), plus the indicators that strategy uses — each
one individually toggleable. Your **entry, stop and target** are drawn as
labelled horizontal lines, with the risk shaded red below the entry and the
reward shaded green above it, so the risk/reward proportion is obvious at a
glance. The specific bars the strategy keyed off (the pullback bar, the
breakout bar, the swing low) are marked; hovering any bar shows its OHLC
and every plotted indicator's value on that day. For RSI Mean Reversion an
RSI-14 subpanel is drawn with both the 35 (entry) and 55 (exit) lines.

The **"Why this signal"** panel on the right has six sections:

1. **Strategy** — which strategy fired, its rule in plain English, and
   where this signal ranked among that day's candidates (so you can tell a
   standout setup from one that only just made the daily cap).
2. **Entry conditions** — every rule the strategy checked, each with the
   actual value, the threshold it was compared against, and pass/fail. This
   is the "why this stock" answer.
3. **Risk checks** — the "6/6" badge expanded into the six named checks
   with real numbers against your limits. A check that was *skipped*
   because the symbol has no sector mapped says so explicitly and links to
   Settings → Sector mappings; it is never shown as a pass.
4. **Position sizing** — the full derivation, not just the answer: risk per
   share, risk budget, the division and the rounding-down that produced the
   share count, the capital committed, and the resulting R:R. Anywhere
   tick-grid snapping or whole-share rounding changed a number, it says so.
5. **Exit plan** — the trailing rule in plain language plus the shared exit
   checks that will apply once the position is open.
6. **Context & warnings** — how far the current price has moved from your
   limit (and whether it may no longer fill), whether you already hold this
   symbol, whether the signal is close to auto-expiring, and — prominently
   — whether you are in Dry Run or Live.

**Approve & place** and **Reject** sit in a footer and go through exactly
the same confirmation dialog and the same code path as the buttons on the
card. Press **Escape** or the **×** to close; **←** and **→** move to the
previous/next pending signal without closing. If a signal is approved,
rejected or auto-expires in another tab while you have it open, the view
updates or closes itself rather than leaving an armed Approve button.

## 7. Placing a Manual Trade

Go to **New Ticket**:

1. Type a symbol — autocomplete searches the instrument cache.
2. Choose direction, order type (market/limit), product, quantity (or use
   the auto-suggested quantity based on your risk settings).
3. Set your stop-loss and target(s), and optionally enable trailing.
4. Add a short rationale note (for your own journal).
5. The form shows a live preview of capital required and risk taken before
   you submit.
6. Submitting runs the same risk checks as strategy signals; if it passes,
   it becomes a pending ticket you then confirm (same as approving a
   strategy signal).

You can edit or delete a ticket while it's still a draft. Once it's
confirmed or executed, it can no longer be edited — rejected/cancelled/
failed tickets can be archived but not un-executed.

## 8. Strategies

Go to **Strategies** to:

- **Enable/disable** each of the three built-in strategies and edit their
  parameters (trend filter length, breakout lookback, stop multiples,
  RSI thresholds, etc. — each field is validated).
- **Scan Now** — manually trigger an entry+exit+breakeven scan immediately,
  instead of waiting for the automated daily run.
- **View version history** — every genuine parameter change is versioned
  with a diff; toggling enable/disable does not create a new version.
- **Signal Queue** — pending signals waiting for your approval.
- **Breakeven Stop Adjustments** — proposals to move a stop to breakeven
  once a trade has gained enough; confirm to have the app move the actual
  broker-side GTT stop for you.
- **Backtest** — pick a date range and symbol set, optionally enable a
  walk-forward consistency check or Monte Carlo simulation, and run a
  historical simulation using the *exact same* entry/exit logic the live
  scanner uses. Results include win rate, average R, profit factor,
  drawdown, Sharpe/Sortino, max consecutive losses, and a trade-by-trade
  table.

### The three built-in strategies, in plain terms

- **Momentum Breakout** — buys stocks breaking out to a new 30-day high on
  heavy volume (3× their normal), while in a long-term uptrend. Uses a wide
  stop and trails it as the trade moves in your favor.
- **Trend Pullback** — buys uptrending stocks that dipped briefly to their
  20-day average and are now turning back up on heavy volume.
- **RSI Mean Reversion** — buys uptrending stocks that got temporarily
  oversold (RSI below 35) and are starting to turn up. **Disabled by
  default** — see the note below.

> **Why these settings changed.** In September 2026 all three strategies were
> re-tested over two years of history *including brokerage, STT and
> slippage*, which the original settings had never accounted for. All three
> were losing money. The heavy-volume requirements and wider stops above are
> the result: both remaining strategies now test profitable, in both halves
> of the sample and under pessimistic cost assumptions. RSI Mean Reversion
> could not be made to work at any setting, so it is switched off — you can
> re-enable it from Strategies to experiment, but nothing in the testing
> supports trading it.
>
> These are backtested results on the symbols you currently follow, not a
> promise. Stay in **Dry Run** until your own Journal shows a positive
> expectancy over 30 or more trades.

## 9. Kill Switch — Emergency Controls

Available from the top mode banner at all times:

- **Cancel All Pending** — instantly rejects every draft/pending ticket.
  Use this if you want to clear the queue without touching open positions.
- **Emergency Exit All** — closes every open position and cancels every
  unfilled entry order. To prevent accidental triggering, you must type the
  exact phrase `EXIT ALL` into a confirmation box before it runs.

## 10. Orders & Trades

A live view of your actual broker order book, trade fills, and GTT
(stop/target) orders — each row is cross-linked back to the ticket that
created it, and each order/GTT can be cancelled directly from here.

## 11. Watchlist

Add or remove symbols to track. The table shows live price, day change %,
trend direction, RSI-14, and distance to the 20-day high — this is also the
universe the strategy scans run against (holdings are always included
automatically).

## 12. Today's Actions

A read-only pre-market briefing: buy candidates ranked by breakout
strength, exit candidates, pending breakeven adjustments, and a "Watch"
list of symbols trending toward a breakout but without a signal yet.

## 13. Journal

Tracks realized results, separately for Dry Run and Live:

- Overall stats: trade count, win rate, expectancy (in R), profit factor,
  total realized P&L, max drawdown, and an equity curve.
- Breakdowns by strategy and by symbol.
- A small-sample-size warning appears below 20 trades — treat early
  statistics with caution.

## 14. Trade Queue & Ticket History

- **Trade Queue** shows all tickets grouped into ready / waiting /
  rejected / executed tabs, plus any open reconciliation mismatches (see
  §15).
- **Ticket History** is the full searchable/filterable audit log — filter
  by status, source, symbol, or date, and optionally include archived
  tickets.

## 15. Reconciliation

Every 3 minutes, the app independently double-checks itself against your
actual broker account: order status, GTT (stop/target order) state, and
open-position quantities. If anything doesn't match — for example a stop
order that triggered outside the app's knowledge — it shows up as a
mismatch that you must explicitly mark reviewed. Nothing is silently
auto-corrected.

## 16. Settings

- **Risk settings** — the limits the risk engine enforces on every trade:
  minimum risk/reward, max capital per trade, max exposure per stock, max
  exposure per sector, max open positions, max total portfolio risk, and
  optionally your starting capital. Changes are tracked with history.
- **Sector mappings** — assign sectors to symbols so the sector-exposure
  risk check can apply to them; an "unmapped symbols" helper shows which
  of your holdings/watchlist symbols still need a sector assigned.

## 17. Daily Routine (Suggested)

1. Log in each morning (required daily by Kite).
2. Check **Today's Actions** for an overview before the market opens.
3. Let **Autopilot** run automatically after market close, or trigger
   **Scan Now** manually from Strategies.
4. Review anything in **Approvals** — confirm, reject, or investigate
   items blocked by risk.
5. Check **Reconciliation** periodically for unresolved mismatches.
6. Review **Journal** stats periodically to evaluate strategy performance.

## 18. Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Session expired" errors | You need to log in again — Kite requires a fresh login every trading day. |
| Live orders rejected by broker | Your static IP is not whitelisted in the Kite Connect developer console. |
| A strategy signal disappeared without action | It expired — signals computed from a stale/older candle are auto-cancelled before the next scan so you never approve against stale prices. |
| A trade shows "protection failed" | The entry order filled but the protective stop (GTT) could not be placed — place a manual stop-loss immediately in Kite or the Orders screen. |
| A reconciliation mismatch appears | The app's records and your broker account disagree (e.g., a stop triggered outside the app). Investigate in Kite directly, then mark it reviewed once resolved. |
