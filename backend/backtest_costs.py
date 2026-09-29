"""PK-21 — Indian equity delivery transaction costs for the historical
backtester.

Pure arithmetic, no I/O. The rates are never hard-coded here: they come from
config.BACKTEST_COST_DEFAULTS (one place), and a backtest run snapshots the
schedule it used into its execution_config so a result is always explainable
against the charges it was computed with.

Judgment calls, documented in docs/OPUS55_PK21_IMPLEMENTATION_REPORT.md:
- Each component is rounded to the paisa. Contract notes round some of them
  (STT, stamp duty) to whole rupees; that rounding is not modelled.
- GST is charged on brokerage + exchange transaction charge + SEBI fee, and
  computed from their unrounded values before its own rounding.
- The DP charge is a flat amount per sell. Every backtest exit is a single
  sell of a single scrip, so it is charged once per exit.
- Every trade is costed as delivery, including the rare same-day round trip
  (an exchange would treat that as intraday, which is cheaper), so the error
  is always in the conservative direction.
"""

from backend import config

COMPONENTS = ("brokerage", "stt", "exchange_txn", "sebi", "gst", "stamp_duty", "dp_charge")


def default_schedule() -> dict:
    return dict(config.BACKTEST_COST_DEFAULTS)


def zero_charges() -> dict:
    return {**{c: 0.0 for c in COMPONENTS}, "total": 0.0}


def compute_charges(side: str, turnover: float, schedule: dict | None = None, enabled: bool = True) -> dict:
    """Charges for ONE order of `turnover` rupees (price x quantity) on
    `side` ("BUY" or "SELL"). Returns every component plus the total, so a
    backtest can report where its costs came from, not just their sum."""
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, not {side!r}")
    if not enabled or turnover <= 0:
        return zero_charges()
    s = schedule or config.BACKTEST_COST_DEFAULTS

    brokerage = turnover * s["brokerage_pct"] / 100
    if s["brokerage_max_per_order"] > 0:
        brokerage = min(brokerage, s["brokerage_max_per_order"])
    stt = turnover * s["stt_pct"] / 100
    exchange_txn = turnover * s["exchange_txn_pct"] / 100
    sebi = turnover * s["sebi_per_crore"] / 10_000_000
    gst = (brokerage + exchange_txn + sebi) * s["gst_pct"] / 100
    stamp_duty = turnover * s["stamp_duty_buy_pct"] / 100 if side == "BUY" else 0.0
    dp_charge = s["dp_charge_per_sell"] if side == "SELL" else 0.0

    out = {
        "brokerage": round(brokerage, 2), "stt": round(stt, 2), "exchange_txn": round(exchange_txn, 2),
        "sebi": round(sebi, 2), "gst": round(gst, 2), "stamp_duty": round(stamp_duty, 2),
        "dp_charge": round(dp_charge, 2),
    }
    out["total"] = round(sum(out[c] for c in COMPONENTS), 2)
    return out


def add_charges(a: dict, b: dict) -> dict:
    out = {c: round(a.get(c, 0.0) + b.get(c, 0.0), 2) for c in COMPONENTS}
    out["total"] = round(sum(out[c] for c in COMPONENTS), 2)
    return out
