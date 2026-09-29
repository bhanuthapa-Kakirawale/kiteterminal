"""Chart + evidence payload behind GET /api/tickets/{id}/chart.

Everything here is read-only and comes out of `ohlc_daily` through
indicators.load_ohlc_frame — the same shared read path the live scans and the
backtester use. This module never calls Kite: a chart the user opens at 09:15
to decide on an 18:22 signal must show exactly the candles that signal was
computed from, and a live historical_data() call would both re-price the
newest bar mid-session and burn a slot in data_jobs.py's 3 req/s queue.

Indicator series are computed via backend/indicators.py, never reimplemented,
for the same reason the backtester shares evaluate_entry: a chart that plots
its own idea of a 200-day SMA can disagree with the number the strategy
actually judged the signal against, which is precisely the question this view
exists to answer.
"""

import json
import logging
from datetime import date

import pandas as pd

from backend import config, execution, indicators, kite_chart, market, trading_calendar
from backend.strategies import STRATEGIES

logger = logging.getLogger(__name__)

# Bars the chart aims to show, plus the warm-up the longest indicator needs
# before its first valid value. A 200-period SMA needs 200 bars before it
# plots at all, so slicing to 250 visible bars would leave the trend filter —
# the one line every strategy's first condition is judged against — blank.
VISIBLE_BARS = 250


def _strategy_module(ticket: dict):
    """The ENTRY module for this ticket, or None.

    Exit tickets carry strategy_name='<name>_exit', which has no module of
    its own; mapping it back to the entry module is what lets an exit signal
    still chart the overlays the position was opened against.
    """
    name = ticket.get("strategy_name") or ""
    if name in STRATEGIES:
        return STRATEGIES[name]
    if name.endswith("_exit") and name[: -len("_exit")] in STRATEGIES:
        return STRATEGIES[name[: -len("_exit")]]
    return None


def _params_for(ticket: dict, module) -> dict:
    """The parameters this signal was actually judged by.

    Read from the evaluation record persisted on the ticket, so a signal
    approved days after someone edited the strategy's parameters is still
    charted against the thresholds that produced it. Falls back to the
    module defaults for manual tickets and for signals created before the
    evaluation record existed.
    """
    evaluation = ticket.get("entry_evaluation")
    if isinstance(evaluation, dict) and isinstance(evaluation.get("params"), dict):
        return dict(evaluation["params"])
    return dict(module.DEFAULT_PARAMS) if module is not None else {}


def _points(dates: pd.Series, values: pd.Series) -> list[dict]:
    """[{time, value}] with NaN points omitted rather than sent as nulls —
    lightweight-charts treats a missing point as a gap, which is what an
    indicator's warm-up period actually is."""
    out = []
    for when, value in zip(dates, values):
        if pd.notna(value):
            out.append({"time": str(when)[:10], "value": round(float(value), 4)})
    return out


def _build_series(df: pd.DataFrame, module, params: dict) -> tuple[list[dict], list[dict]]:
    """(overlays, subpanels) for this strategy.

    Overlays share the price axis; subpanels get their own. Each carries the
    period it was computed with so the UI can label it honestly ("SMA-200")
    instead of hardcoding a number that a parameter change would falsify.
    """
    close, dates = df["close"], df["date"]
    name = module.STRATEGY_NAME if module is not None else None
    trend_period = int(params.get("trend_filter_period", 200))

    overlays = [{
        "key": "sma_trend", "label": f"SMA-{trend_period}", "kind": "line", "period": trend_period,
        "color_token": "--accent", "data": _points(dates, indicators.sma(close, trend_period)),
        "description": "Long-term trend filter — every strategy requires the close above this.",
    }]
    subpanels = []

    if name == "momentum_breakout":
        period = int(params.get("donchian_lookback", 20))
        overlays.append({
            "key": "donchian_high", "label": f"{period}-day Donchian high", "kind": "line", "period": period,
            "color_token": "--accent-amber",
            "data": _points(dates, indicators.donchian_high_excl_today(close, period)),
            "description": f"Highest close of the prior {period} days, excluding each bar itself — "
                           f"the level the breakout must clear.",
        })
    elif name == "trend_pullback":
        period = int(params.get("pullback_ma_period", 20))
        band_pct = float(params.get("pullback_band_pct", 1.5))
        ema = indicators.ema(close, period)
        overlays.append({
            "key": "ema_pullback", "label": f"EMA-{period}", "kind": "line", "period": period,
            "color_token": "--accent-amber", "data": _points(dates, ema),
            "description": "The moving average price must pull back to and then recover above.",
        })
        overlays.append({
            "key": "pullback_band", "label": f"{period}-EMA +{band_pct}% band", "kind": "line", "period": period,
            "color_token": "--violet", "data": _points(dates, ema * (1 + band_pct / 100)),
            "description": f"A bar's low reaching below this counts as a touch of the {period}-EMA.",
        })
    elif name == "rsi_mean_reversion":
        rsi_period = 14
        oversold = float(params.get("rsi_oversold_threshold", 35))
        exit_level = float(params.get("rsi_exit_level", 55))
        subpanels.append({
            "key": "rsi", "label": f"RSI-{rsi_period}", "kind": "line", "period": rsi_period,
            "color_token": "--violet", "data": _points(dates, indicators.wilders_rsi(close, rsi_period)),
            "scale": {"min": 0, "max": 100},
            # Both thresholds are drawn because they bracket the whole trade:
            # the entry only fires below `oversold`, and the strategy's own
            # exit fires at or above `exit_level`.
            "guides": [
                {"value": oversold, "label": f"{oversold:g} — entry threshold", "color_token": "--pnl-positive"},
                {"value": exit_level, "label": f"{exit_level:g} — RSI-recovery exit", "color_token": "--pnl-negative"},
            ],
            "description": "Wilder's RSI-14. Entry requires a reading below the entry threshold; "
                           "the strategy's own exit fires once it recovers to the upper line.",
        })
        swing_period = int(params.get("swing_low_lookback_days", 10))
        overlays.append({
            "key": "swing_low", "label": f"{swing_period}-day swing low", "kind": "line", "period": swing_period,
            "color_token": "--pnl-negative",
            "data": _points(dates, df["low"].rolling(window=swing_period).min()),
            "description": f"Lowest low of the trailing {swing_period} days — one of the two "
                           f"candidates for this strategy's stop.",
        })

    # Volume + its average is shown for every strategy: it is momentum_breakout's
    # third entry condition, and useful context everywhere else.
    vol_period = 20
    subpanels.append({
        "key": "volume", "label": "Volume", "kind": "histogram", "period": None,
        "color_token": "--text-muted", "data": _points(dates, df["volume"]),
        "description": "Daily traded volume.",
        "companion": {
            "key": "avg_volume", "label": f"Avg volume ({vol_period})", "kind": "line", "period": vol_period,
            "color_token": "--accent-amber",
            "data": _points(dates, indicators.avg_volume(df["volume"], vol_period)),
        },
    })
    return overlays, subpanels


def _staleness(signal_date: str | None, last_candle_date: str | None) -> dict:
    """How close this signal is to being auto-expired.

    Expiry is driven by the SCANNER (strategy_engine.expire_stale_signals
    cancels any pending entry signal computed from an older candle than the
    one being scanned), and separately a symbol whose newest candle is more
    than MAX_DATA_STALENESS_DAYS behind the last completed session is skipped
    entirely. Both are reported so the UI can warn before the user invests
    thought in a signal that is about to vanish.
    """
    last_session = market.last_complete_session_date()
    result = {
        "signal_date": signal_date,
        "last_candle_date": last_candle_date,
        "last_session_date": last_session.isoformat(),
        "max_staleness_days": config.MAX_DATA_STALENESS_DAYS,
        "signal_age_sessions": None,
        "candle_age_days": None,
        "data_is_stale": False,
        "expires_on_next_scan": False,
        "warning": None,
    }
    if last_candle_date:
        age = (last_session - date.fromisoformat(str(last_candle_date)[:10])).days
        result["candle_age_days"] = age
        result["data_is_stale"] = age > config.MAX_DATA_STALENESS_DAYS
    if signal_date:
        signal_day = date.fromisoformat(str(signal_date)[:10])
        # Sessions, not calendar days: a Friday signal read on Monday evening
        # is one session old, and an exchange holiday is not a session (PK-22C).
        result["signal_age_sessions"] = trading_calendar.trading_days_between(signal_day, last_session)
        # A signal computed from an older candle than the latest available one
        # is cancelled the moment the next scan runs — its limit price and stop
        # came from a close that is no longer current.
        if last_candle_date and signal_day < date.fromisoformat(str(last_candle_date)[:10]):
            result["expires_on_next_scan"] = True
            result["warning"] = (
                f"This signal was computed from the {signal_date} candle, but a newer "
                f"{last_candle_date} candle already exists — it will be auto-expired as soon as "
                f"the next scan runs, because its price levels are no longer current."
            )
        elif result["data_is_stale"]:
            result["warning"] = (
                f"The newest candle for this symbol is {last_candle_date}, {result['candle_age_days']} days "
                f"behind the last completed session — beyond the {config.MAX_DATA_STALENESS_DAYS}-day limit."
            )
    return result


def _levels(ticket: dict) -> dict:
    """Read straight off the ticket, never recomputed.

    These are already snapped to the exchange tick grid by
    strategy_engine._create_entry_ticket, and they are the exact prices the
    broker will be sent. Recomputing them here from `close × buffer` would
    reintroduce the un-snapped values and let the chart draw a stop the user
    will never actually get.
    """
    entry, stop, target = ticket["entry_price"], ticket["stop_loss"], ticket["target_1"]
    risk_per_share = abs(entry - stop) if entry is not None and stop is not None else None
    return {
        "entry": entry,
        "stop": stop,
        "target": target,
        "target_2": ticket.get("target_2"),
        "trailing_enabled": bool(ticket.get("trailing_enabled")),
        "trailing_distance": ticket.get("trailing_distance"),
        "risk_per_share": risk_per_share,
        "risk_reward_ratio": ticket.get("risk_reward_ratio"),
        "quantity": ticket.get("quantity"),
        "transaction_type": ticket.get("transaction_type"),
    }


def _sizing(ticket: dict, settings) -> dict:
    """The position-size derivation, step by step rather than just the answer.

    Every number is recomputed from what the ticket and the evaluation record
    already hold, so this can never quote a quantity the ticket doesn't have.
    The two places a number visibly changes — floor() on the share count and
    the tick-grid snap on the three price levels — are reported explicitly,
    because a stop that reads ₹9,036.50 when the formula said ₹9,036.43 is
    exactly the kind of small discrepancy that makes a user distrust the
    whole screen if it appears without explanation.
    """
    evaluation = ticket.get("entry_evaluation") if isinstance(ticket.get("entry_evaluation"), dict) else {}
    stored = evaluation.get("sizing") or {}
    raw_levels = evaluation.get("levels_raw") or {}
    tick = evaluation.get("tick_size")

    entry, stop, quantity = ticket["entry_price"], ticket["stop_loss"], ticket["quantity"]
    risk_per_share = abs(entry - stop)
    portfolio_value = stored.get("portfolio_value")
    risk_pct = settings["max_risk_per_trade_pct"] if settings is not None else None
    risk_budget = portfolio_value * risk_pct / 100 if portfolio_value and risk_pct is not None else None

    steps = [
        {"label": "Risk per share", "formula": f"entry − stop = {entry:,.2f} − {stop:,.2f}",
         "value": round(risk_per_share, 2), "unit": "inr"},
    ]
    if risk_budget is not None:
        steps.append({
            "label": "Risk budget",
            "formula": f"{risk_pct}% of portfolio {portfolio_value:,.0f}",
            "value": round(risk_budget, 2), "unit": "inr",
        })
        steps.append({
            "label": "Quantity",
            "formula": f"floor({risk_budget:,.0f} / {risk_per_share:,.2f})"
                       + (f" = floor({risk_budget / risk_per_share:.2f})" if risk_per_share else ""),
            "value": quantity, "unit": "count",
            "note": _quantity_note(stored, quantity, risk_budget, risk_per_share),
        })
    else:
        steps.append({"label": "Quantity", "formula": "as sized at signal time",
                      "value": quantity, "unit": "count"})

    steps.append({"label": "Capital", "formula": f"{quantity} × {entry:,.2f}",
                  "value": round(entry * quantity, 2), "unit": "inr"})
    steps.append({"label": "Risk", "formula": f"{quantity} × {risk_per_share:,.2f}",
                  "value": round(risk_per_share * quantity, 2), "unit": "inr"})
    rr = ticket.get("risk_reward_ratio")
    steps.append({
        "label": "R : R",
        "formula": f"(target − entry) / (entry − stop) = "
                   f"({ticket['target_1']:,.2f} − {entry:,.2f}) / {risk_per_share:,.2f}",
        "value": None if rr is None else round(rr, 2), "unit": "ratio",
    })

    # Where tick-grid snapping moved a price, and by how much.
    adjustments = []
    for label, raw_key, final in (("Entry", "entry_price", entry), ("Stop", "stop_loss", stop),
                                  ("Target", "target_1", ticket["target_1"])):
        raw = raw_levels.get(raw_key)
        if raw is not None and abs(raw - final) > 1e-9:
            adjustments.append({
                "label": label, "from": round(raw, 4), "to": final,
                "reason": f"snapped onto the ₹{tick} tick grid" if tick else "snapped onto the tick grid",
            })
    return {
        "steps": steps,
        "tick_size": tick,
        "adjustments": adjustments,
        "portfolio_value": portfolio_value,
        "max_risk_per_trade_pct": risk_pct,
        "entry_price_buffer_pct": stored.get("entry_price_buffer_pct", config.ENTRY_PRICE_BUFFER_PCT),
        "signal_close": evaluation.get("signal_close"),
    }


def _quantity_note(stored: dict, quantity: int, risk_budget: float, risk_per_share: float) -> str | None:
    """Which of the two limits actually bound the size, and what the floor()
    threw away — a user comparing "₹954 budget / ₹318 risk" against a
    quantity of 3 should not have to work out where the remainder went."""
    notes = []
    risk_qty, capital_qty = stored.get("risk_based_qty"), stored.get("capital_based_qty")
    if risk_qty is not None and capital_qty is not None:
        binding = "risk-per-trade" if risk_qty <= capital_qty else "capital-per-trade"
        notes.append(f"{binding} limit binds ({risk_qty} vs {capital_qty} shares)")
    if risk_per_share:
        unused = risk_budget - quantity * risk_per_share
        if unused > 0.5:
            notes.append(f"rounding down to a whole share leaves ₹{unused:,.0f} of the budget unused")
    return "; ".join(notes) if notes else None


def _exit_plan(ticket: dict, module, params: dict) -> dict:
    """The exits that will apply once this position is open, in plain language.

    'armed' is deliberately NOT computed here. The thesis-invalidation exit
    only arms once price has closed above its EMA at or after entry
    (strategies/shared.shared_exit_checks) — for a signal that has not been
    executed yet there is no entry date, so any boolean would be an invention.
    The rule is stated instead, with when it will start applying.
    """
    thesis_ma = params.get("thesis_invalidation_ma")
    time_exit_days = params.get("time_exit_days")
    # PK-19: "the position is open" is the server-derived lifecycle's call
    # when the route supplied one (backend/lifecycle.py).
    lc = ticket.get("lifecycle")
    is_open = (lc["is_holding"] if lc else
               ticket.get("status") == "executed" and ticket.get("closed_at") is None)
    rules = []

    if module is not None and module.STRATEGY_NAME == "rsi_mean_reversion":
        rules.append({
            "name": "RSI recovery exit",
            "detail": f"Checked FIRST, before the shared exits: the position is closed once RSI-14 "
                      f"reaches {params.get('rsi_exit_level')}, taking the reversion profit.",
        })
    if thesis_ma:
        rules.append({
            "name": "Thesis invalidation",
            "detail": f"Exits if the close falls back below the {thesis_ma}-EMA. This check is "
                      + ("armed only once price has closed above that EMA at or after entry."
                         if is_open else
                         "not armed yet — it only starts applying once price has closed above that "
                         f"{thesis_ma}-EMA at or after entry, so a trade that legitimately begins "
                         "below it is not exited immediately."),
        })
    if time_exit_days:
        rules.append({
            "name": "Time exit",
            "detail": f"Exits if the position is held more than {time_exit_days} trading days "
                      f"without hitting either the stop or the target.",
        })

    if ticket.get("trailing_enabled"):
        trailing = (
            f"Once price touches the target, the stop starts trailing ₹{ticket['trailing_distance']:,.2f} "
            f"below each day's close, ratcheting upward only — it never moves back down."
        )
    else:
        trailing = (
            "No trailing stop. This strategy takes profit at a fixed target or on its own exit "
            "condition, so the stop and target are placed as a single OCO pair and left alone."
            if module is not None and module.STRATEGY_NAME == "rsi_mean_reversion"
            else "No trailing stop is configured for this ticket."
        )

    return {"trailing": trailing, "shared_rules": rules}


def build_payload(ticket: dict, settings=None) -> dict:
    """The whole Signal Detail payload for one ticket.

    `settings` is the account's risk_settings row, used only to show the
    position-sizing derivation — passed in by the route rather than read here
    so this module stays free of its own DB writes and account resolution.
    """
    module = _strategy_module(ticket)
    params = _params_for(ticket, module)
    trend_period = int(params.get("trend_filter_period", 200))

    df = indicators.load_ohlc_frame(ticket["instrument_token"])
    bars_available = len(df)
    # Enough warm-up for the longest indicator, plus the visible window.
    wanted = trend_period + VISIBLE_BARS

    diagnostic = None
    if bars_available == 0:
        diagnostic = {
            "code": "NO_HISTORY",
            "message": f"No daily candles are stored for {ticket['symbol']} yet. Add it to the watchlist "
                       f"and let the daily sync backfill its history, then reopen this view.",
            "bars_available": 0, "bars_required": trend_period,
        }
    elif bars_available < trend_period:
        diagnostic = {
            "code": "INSUFFICIENT_HISTORY",
            "message": f"Only {bars_available} daily candles are stored for {ticket['symbol']}, fewer than the "
                       f"{trend_period} a {trend_period}-period trend filter needs. The candles below are real, "
                       f"but the SMA-{trend_period} line cannot be plotted for them.",
            "bars_available": bars_available, "bars_required": trend_period,
        }

    if bars_available:
        # Indicators are computed over the FULL history and only then sliced —
        # computing them on a pre-sliced window would restart every moving
        # average from inside the window and silently plot wrong values.
        overlays, subpanels = _build_series(df, module, params)
        visible = df.tail(wanted)
        first_visible = str(visible["date"].iloc[0])[:10]
        candles = [
            {"time": str(r.date)[:10], "open": float(r.open), "high": float(r.high),
             "low": float(r.low), "close": float(r.close)}
            for r in visible.itertuples()
        ]
        for series in (*overlays, *subpanels):
            series["data"] = [p for p in series["data"] if p["time"] >= first_visible]
            companion = series.get("companion")
            if companion:
                companion["data"] = [p for p in companion["data"] if p["time"] >= first_visible]
        last_candle_date = str(df["date"].iloc[-1])[:10]
    else:
        overlays, subpanels, candles, last_candle_date = [], [], [], None

    evaluation = ticket.get("entry_evaluation") if isinstance(ticket.get("entry_evaluation"), dict) else None
    chart_link = kite_chart.fallback_chain(
        ticket["symbol"], ticket.get("exchange") or "NSE",
        ticket.get("instrument_token"), ticket.get("kite_chart_status") or kite_chart.STATUS_OK,
    )

    return {
        "ticket_id": ticket["id"],
        "symbol": ticket["symbol"],
        "exchange": ticket.get("exchange") or "NSE",
        "instrument_token": ticket.get("instrument_token"),
        "transaction_type": ticket.get("transaction_type"),
        "status": ticket.get("status"),
        # PK-19: the ticket/order state and the position lifecycle, as two
        # separate concepts (an executed exit is not a closed position).
        "ticket_state": ticket.get("ticket_state"),
        "lifecycle": ticket.get("lifecycle"),
        "strategy_name": ticket.get("strategy_name"),
        "strategy_label": (evaluation or {}).get("strategy_label")
        or (getattr(module, "STRATEGY_LABEL", None) if module else None)
        or ticket.get("strategy_name"),
        # Served rather than hardcoded in app.js, so a fourth strategy module
        # shows up in this panel without the frontend being touched (D1).
        "strategy_description": getattr(module, "STRATEGY_DESCRIPTION", None) if module else None,
        "rationale": ticket.get("rationale"),
        "candles": candles,
        "overlays": overlays,
        "subpanels": subpanels,
        "levels": _levels(ticket),
        "conditions": (evaluation or {}).get("conditions", []),
        "markers": (evaluation or {}).get("markers", []),
        "rank": (evaluation or {}).get("rank") or {},
        "params": params,
        "sizing": _sizing(ticket, settings),
        "exit_plan": _exit_plan(ticket, module, params),
        "signal_candle_date": ticket.get("signal_date"),
        "freshness": _staleness(ticket.get("signal_date"), last_candle_date),
        # The mode this ticket executes in: its own once stamped (an exit
        # inherits its position's, PK-3), else the global one for a new entry.
        "execution_mode": ticket.get("mode") or execution.get_execution_mode(),
        "kite_chart": {
            "url": ticket.get("kite_chart_url"),
            "status": ticket.get("kite_chart_status"),
            "options": chart_link,
        },
        "bars_available": bars_available,
        "diagnostic": diagnostic,
    }


def parse_evaluation(raw: str | None) -> dict | None:
    """Tolerant read of the stored JSON — a malformed record should degrade
    the evidence panel, never break the ticket list it is attached to."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Could not parse entry_evaluation JSON")
        return None
    return value if isinstance(value, dict) else None
