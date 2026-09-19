"""Structured entry-evaluation records, shared by every strategy module.

Each strategy's `evaluate_entry` already returned a `checklist` dict whose
keys and nesting were strategy-specific (`checklist["pullback"]["ema_20_at_touch"]`
and friends) — perfectly readable by a human reading that one module, but
impossible for a UI to render generically, which is exactly why each module
also carried its own hand-written `build_entry_rationale` with those key
names baked in. That made the Approvals card's one-line description and any
richer explanation two separate sources of truth that could disagree.

This module defines the one flat, uniform row shape both now render from:

    {name, description, actual_value, threshold, operator, passed}

plus presentational siblings (`value_label`, `threshold_label`, `detail`,
`summary`) so a caller never has to know which condition it is looking at in
order to format it. `build_rationale` below turns the same list into the
one-line sentence stored on the ticket, so the card and the Signal Detail
panel are literally reading the same record.

The nested `checklist` is deliberately left untouched alongside this — it is
also written by exit tickets and alert-action tickets, and backend/backtest.py
reads `checklist["rsi_recovery"]["pass"]` et al. to classify exit reasons.
"""


def inr(value: float | None) -> str:
    """₹1,23,456.78-style rendering is deliberately NOT used — the rest of the
    app's Python-side strings (strategy rationales, risk-check messages) all
    use plain thousands grouping, and mixing the two in one sentence reads
    worse than either alone."""
    return "—" if value is None else f"₹{value:,.2f}"


def num(value: float | None, places: int = 2) -> str:
    return "—" if value is None else f"{value:,.{places}f}"


def multiple(value: float | None, places: int = 1) -> str:
    return "—" if value is None else f"{value:,.{places}f}×"


def condition(
    name: str,
    description: str,
    *,
    actual_value: float | None,
    threshold: float | None,
    operator: str,
    passed: bool,
    value_label: str,
    threshold_label: str,
    detail: str | None = None,
) -> dict:
    """One row of the entry-evaluation record.

    `actual_value`/`threshold` are the raw numbers (for the UI to compare,
    chart, or re-check); `value_label`/`threshold_label` are the same numbers
    already worded for display ("close ₹9,345.00", "SMA-200 ₹8,102.40"), so
    the frontend never has to know that this particular row happens to be a
    price rather than a ratio. `detail` carries anything that isn't a simple
    two-sided comparison (which bar the pullback touched on, how far above a
    level the breakout closed).

    A compound condition (trend_pullback's "close > prev close AND > EMA")
    reports the binding side as `threshold` and spells the full rule out in
    `description` — collapsing it into two rows would let the panel show
    "PASS / PASS" for a rule that is only ever satisfied as a pair.
    """
    summary = f"{name}: {value_label} {operator} {threshold_label}"
    if detail:
        summary = f"{summary} ({detail})"
    return {
        "name": name,
        "description": description,
        "actual_value": None if actual_value is None else float(actual_value),
        "threshold": None if threshold is None else float(threshold),
        "operator": operator,
        "passed": bool(passed),
        "value_label": value_label,
        "threshold_label": threshold_label,
        "detail": detail,
        "summary": summary,
    }


def marker(kind: str, date, price: float | None, label: str, detail: str | None = None) -> dict:
    """A specific bar the strategy keyed off, for the detail view's chart to
    annotate. These come out of the SAME evaluation that produced the signal
    (trend_pullback already located its touch bar to decide `pullback_ok`),
    never from a second pass over the data — re-deriving "which bar was the
    pullback" in the view layer is how a chart ends up confidently marking a
    different bar than the one the signal actually fired on.
    """
    return {
        "kind": kind,
        "date": None if date is None else str(date)[:10],
        "price": None if price is None else float(price),
        "label": label,
        "detail": detail,
    }


def build_rationale(strategy_label: str, conditions: list[dict]) -> str:
    """The one-line description shown on the Approvals card, generated from
    the same rows the Signal Detail panel itemises. Every condition is
    included, not just the passing ones: this is only ever called for a
    candidate that passed (all rows are PASS), and filtering by `passed`
    would silently produce a shorter, misleading sentence if that ever
    stopped being true."""
    if not conditions:
        return f"{strategy_label}: no entry conditions recorded."
    return f"{strategy_label} — " + "; ".join(c["summary"] for c in conditions) + "."


def all_passed(conditions: list[dict]) -> bool:
    """Every module's overall `passed` must equal the AND of its own rows —
    tested in tests/test_signal_detail.py so a future condition added to a
    module's list but left out of its `passed` expression (or vice versa)
    fails loudly rather than producing a panel that shows a failing row on an
    approved signal."""
    return all(c["passed"] for c in conditions)
