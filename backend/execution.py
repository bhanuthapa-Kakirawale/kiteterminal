import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException, OrderException, PermissionException

from backend import config, instruments, market
from backend.db import get_connection
from backend.ticker import ticker_manager


def get_execution_mode() -> str:
    conn = get_connection()
    try:
        row = conn.execute("SELECT mode FROM execution_settings WHERE id = 1").fetchone()
        return row["mode"]
    finally:
        conn.close()


def set_execution_mode(mode: str, confirm_real_money: bool, account_id: int | None = None) -> str:
    if mode == "live" and not confirm_real_money:
        raise ValueError("confirm_real_money must be true to switch to live mode")
    previous_mode = get_execution_mode()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE execution_settings SET mode = ?, updated_at = datetime('now') WHERE id = 1",
            (mode,),
        )
        if account_id is not None and mode != previous_mode:
            from backend import system_events
            system_events.log_event(conn, account_id, "mode_switched", f"{previous_mode} -> {mode}")
        conn.commit()
    finally:
        conn.close()
    if mode != previous_mode:
        # PK-11: every open tab updates the mode it displays. A convenience
        # only — a stale tab is still refused at confirm (expected_mode).
        ticker_manager.broadcast({"type": "mode_changed", "mode": mode, "previous_mode": previous_mode})
    return mode


# ---- PK-2: broker submission tracking ---------------------------------------
#
# trade_tickets.submission_state records what is known about a live ticket's
# broker order, separately from its lifecycle status:
#
#   NULL          never submitted (draft/pending/rejected tickets, dry runs,
#                 and every ticket from before PK-2)
#   submitting    execution claimed (status 'confirmed'); the broker call is
#                 about to start or in flight. Written in the same
#                 compare-and-set as pending_confirmation -> confirmed.
#   acknowledged  the broker returned an order id (status 'executed')
#   rejected      the broker definitively rejected the order (status 'failed')
#   unknown       the call failed in a way that does not prove rejection:
#                 the order may or may not exist. Status stays 'confirmed';
#                 order_monitor searches Kite for the order tag.
#   adopted       an unknown submission whose tagged broker order was found
#                 and taken over (status 'executed', normal lifecycle)
#   not_found     no tagged order appeared within the reconciliation window:
#                 no order was placed (status 'failed')
#   needs_review  the broker evidence is contradictory (several or
#                 mismatching tagged orders) or can no longer be checked.
#                 Status stays 'confirmed'; automation stops touching it.
SUBMITTING = "submitting"
ACKNOWLEDGED = "acknowledged"
REJECTED = "rejected"
UNKNOWN = "unknown"
ADOPTED = "adopted"
NOT_FOUND = "not_found"
NEEDS_REVIEW = "needs_review"

# Only these prove that Kite refused the order: it answered with an explicit
# validation/RMS/permission error. Everything else that can escape
# place_order — NetworkException (gateway timeout), TokenException,
# GeneralException (5xx / unrecognised error_type), DataException
# (unparseable body), raw requests errors (kiteconnect re-raises them
# unwrapped), KeyError/TypeError from a malformed payload — leaves open the
# possibility that the order was accepted, so it is an UNKNOWN outcome.
DEFINITE_REJECTIONS = (OrderException, InputException, PermissionException)

KITE_TAG_MAX_LENGTH = 20
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(n: int) -> str:
    digits = ""
    while True:
        n, r = divmod(n, 36)
        digits = _BASE36[r] + digits
        if n == 0:
            return digits


def order_tag_for(ticket_id: int, created_at: str) -> str:
    """The Kite order tag for a ticket's order: 'kt<id>x<created_at as
    base36 epoch seconds>'. Deterministic from the ticket row, so it survives
    a restart even without the copy persisted in trade_tickets.order_tag. The
    created_at part keeps it unique if a restored database backup reissues a
    ticket id that already tagged an order the same day. Kite allows at most
    20 alphanumeric characters."""
    created = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    tag = f"kt{ticket_id}x{_base36(int(created.timestamp()))}"
    if len(tag) > KITE_TAG_MAX_LENGTH or not tag.isalnum():
        raise ValueError(f"Order tag {tag!r} does not satisfy Kite's tag rules")
    return tag


def utc_now_sql() -> str:
    """Now, in the text format SQLite's datetime('now') uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def update_ticket_if(
    conn: sqlite3.Connection, ticket_id: int, *,
    status_in: tuple[str, ...], submission_state_in: tuple[str | None, ...], **columns,
) -> bool:
    """Compare-and-set for the writes that follow the claim: applies
    `columns` only if the ticket is still in one of status_in AND one of
    submission_state_in (None matches NULL). Since PK-2 both the confirming
    request and order_monitor can resolve the same live ticket, so neither
    writes unconditionally — the loser gets False, must not overwrite, and
    re-reads the ticket. Column names come from code, never from input."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    states = [s for s in submission_state_in if s is not None]
    conditions = []
    if states:
        conditions.append(f"submission_state IN ({', '.join('?' * len(states))})")
    if None in submission_state_in:
        conditions.append("submission_state IS NULL")
    cursor = conn.execute(
        f"UPDATE trade_tickets SET {assignments} "
        f"WHERE id = ? AND status IN ({', '.join('?' * len(status_in))}) AND ({' OR '.join(conditions)})",
        (*columns.values(), ticket_id, *status_in, *states),
    )
    return cursor.rowcount == 1


# ---- PK-8: explicit exit lifecycle ------------------------------------------
#
# Placing an exit order is not exiting. A live exit ticket goes
#   confirmed (claimed / UNKNOWN) -> executed + entry_fill_status 'pending'
# once Kite accepts the order: EXITING. The position it closes stays OPEN,
# keeps its protective GTT and is not journaled until order_monitor sees the
# broker report the full quantity COMPLETE ('filled'); only then — and only
# if that leaves the position flat — is the position's closed_at stamped.
# PK-13: an exit that ends after a partial fill ('partial') sold exactly its
# filled_quantity; the rest of the position stays open, and the exit no
# longer owns it. 'unconfirmed' exits still keep it and need a human. A
# dry-run exit is simulated as filled at once. No new status value:
# 'executed' + 'pending' keeps the exit owning its position under the PK-4
# unique index while its order is in flight (EXIT_IN_FLIGHT_SQL).
#
# An exit whose fill is proven. NULL covers exits from before fills were
# tracked; their positions already carry closed_at.
EXIT_FILL_PROVEN_SQL = "COALESCE(entry_fill_status, 'filled') = 'filled'"

# PK-13: an exit that OWNS its position — its order is still in flight:
# claimed / UNKNOWN ('confirmed'), EXITING ('executed' + 'pending') or
# dropped off the order book unresolved ('unconfirmed'). Exactly the
# predicate of db.idx_trade_tickets_one_exit_owner. An exit whose order has
# ended — 'filled', or 'partial' (ended after a partial fill) — no longer
# owns it: the shares it sold are subtracted from the position's open
# quantity (backend/position_quantity.py) and the rest may be exited anew.
EXIT_IN_FLIGHT_SQL = (
    "(status = 'confirmed' OR (status = 'executed' AND COALESCE(entry_fill_status, '') IN ('pending', 'unconfirmed')))"
)

_EXIT_STATES = {"pending": "exiting", "partial": "exit_partial", "unconfirmed": "exit_unconfirmed"}


def exit_state(ticket) -> str | None:
    """'exiting' (order at the broker, awaiting its fill), 'exit_partial'
    (the order ended after a partial fill: those shares are sold, the rest
    of the position is still open — PK-13), 'exit_unconfirmed' (needs
    review) or 'exited' for an executed closing sell; None otherwise."""
    if ticket["status"] != "executed" or not is_closing_sell(ticket):
        return None
    return _EXIT_STATES.get(ticket["entry_fill_status"], "exited")


def supersede_sibling_exits(conn: sqlite3.Connection, position_id: int, owner_id: int) -> list[int]:
    """Once exit ticket owner_id has closed position_id — its full fill is
    proven (PK-8) — every other exit of that position still awaiting
    approval is stale: each is rejected (compare-and-set, so a sibling
    claimed meanwhile is left alone) so it can no longer be approved. Never
    called on a claim, an UNKNOWN outcome or a merely accepted order: those
    leave the siblings pending (and blocked, PK-4). The caller commits."""
    candidates = conn.execute(
        "SELECT id FROM trade_tickets WHERE closes_ticket_id = ? AND id != ? "
        "AND status IN ('draft', 'pending_confirmation') ORDER BY id",
        (position_id, owner_id),
    ).fetchall()
    superseded = []
    for row in candidates:
        if conn.execute(
            "UPDATE trade_tickets SET status = 'rejected' WHERE id = ? AND status IN ('draft', 'pending_confirmation')",
            (row["id"],),
        ).rowcount == 1:
            conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'rejected', ?)",
                (row["id"], f"Superseded by exit ticket {owner_id} — position {position_id} is closed; "
                            f"no order was sent."),
            )
            superseded.append(row["id"])
    return superseded


def compute_risk_metrics(ticket_like: dict) -> dict:
    entry = ticket_like["entry_price"]
    qty = ticket_like["quantity"]
    stop_loss = ticket_like["stop_loss"]
    target_1 = ticket_like["target_1"]
    target_2 = ticket_like.get("target_2")

    capital = entry * qty
    risk_per_share = abs(entry - stop_loss)
    risk_amount = risk_per_share * qty
    # Risk:reward is computed against target_1 only — the schema has no
    # partial-exit/quantity-split modeling, so target_2's ratio is informational
    # only and is never persisted.
    risk_reward_ratio = abs(target_1 - entry) / risk_per_share if risk_per_share else None
    target_2_risk_reward_ratio = (
        abs(target_2 - entry) / risk_per_share if target_2 and risk_per_share else None
    )
    return {
        "capital": capital,
        "risk_amount": risk_amount,
        "risk_reward_ratio": risk_reward_ratio,
        "target_2_risk_reward_ratio": target_2_risk_reward_ratio,
    }


@dataclass
class ExecutionResult:
    success: bool
    kite_order_id: str | None
    kite_gtt_id: int | None
    # 'executed' | 'failed' | 'confirmed' (outcome unknown: the ticket stays
    # claimed until order_monitor resolves it from broker evidence)
    status: str
    event_type: str  # 'executed' | 'execution_failed' | 'protection_failed' | 'submission_unknown'
    message: str
    order_variety: str | None = None  # 'regular' | 'amo'
    # 'pending' for a live order until backend/order_monitor.py sees the
    # broker's terminal state; 'filled' for a simulated (dry-run) execution.
    entry_fill_status: str | None = None


def is_closing_sell(ticket_row) -> bool:
    """Single source of truth for 'this SELL ticket reduces/exits a position
    rather than opening one' — used here (skip the protective GTT, exit via
    _execute_exit not _execute_live), by order_monitor.py (route a filled
    order through _resolve_exit not _resolve_entry), and by risk_engine.py
    (skip the six position-opening checks). A ticket with closes_ticket_id
    set (Phase 5 strategy exits, the kill switch's emergency exit) is always
    closing. source='alert_action' SELLs are also always closing — the
    Quick Action popup's sell endpoint (routes/tickets.py) only ever
    constructs a ticket that reduces/exits a currently-held position, never
    one that opens a short."""
    return ticket_row["transaction_type"] == "SELL" and (
        ticket_row["closes_ticket_id"] is not None or ticket_row["source"] == "alert_action"
    )


def execute_ticket(kite: KiteConnect, ticket: sqlite3.Row, entry_variety: str | None = None) -> ExecutionResult:
    """entry_variety ('regular' | 'amo') is the order variety the account
    safety gate decided for a live NEW entry (PK-12H) — an AMO only with the
    approver's explicit consent. It is never re-derived from the clock here.
    Exits keep resolving their own variety (AMO outside market hours)."""
    if ticket["mode"] == "dry_run":
        return _simulate(ticket)
    if is_closing_sell(ticket):
        return _execute_exit(kite, ticket)
    return _execute_live(kite, ticket, _entry_variety(kite, entry_variety))


def _entry_variety(kite: KiteConnect, entry_variety: str | None) -> str:
    if entry_variety == "regular":
        return kite.VARIETY_REGULAR
    if entry_variety == "amo":
        return kite.VARIETY_AMO
    # Backstop: every live entry is confirmed through the account safety
    # gate, which always decides the variety. Refuse before any broker call.
    raise ValueError(f"No order variety was decided for this live entry ({entry_variety!r}); nothing was sent")


def _order_tag(ticket: sqlite3.Row) -> str:
    return ticket["order_tag"] or order_tag_for(ticket["id"], ticket["created_at"])


def unknown_result(ticket: sqlite3.Row, exc: BaseException, variety: str | None = None) -> ExecutionResult:
    """The broker call failed without proving that Kite rejected the order.
    Never retried here: order_monitor resolves it by the order tag."""
    what = "Exit" if is_closing_sell(ticket) else "Entry"
    return ExecutionResult(
        success=False, kite_order_id=None, kite_gtt_id=None,
        status="confirmed", event_type="submission_unknown",
        message=(
            f"{what} order outcome UNKNOWN — DO NOT RESUBMIT. The broker call failed with "
            f"{type(exc).__name__}: {exc}. Kite may or may not have accepted the order. The order monitor is "
            f"searching Kite's order book for tag {_order_tag(ticket)} and will take the order over if it "
            f"exists; nothing will be resubmitted automatically."
        ),
        order_variety=variety,
    )


def _simulate(ticket: sqlite3.Row) -> ExecutionResult:
    return ExecutionResult(
        success=True,
        kite_order_id=None,
        kite_gtt_id=None,
        status="executed",
        event_type="executed",
        message="Simulated (dry run) — no real order placed.",
        entry_fill_status="filled",
    )


def resolve_exit_side(transaction_type: str) -> str:
    return "SELL" if transaction_type == "BUY" else "BUY"


def resolve_order_variety(kite: KiteConnect, at: datetime | None = None) -> str:
    # Exits only (PK-12H): an exit approved outside market hours still goes
    # out as an AMO — reducing risk stays possible, and PK-8 keeps the
    # position's protective GTT until the exit fills. New entries never come
    # here; their variety is decided by the account safety gate.
    if config.USE_AMO_WHEN_MARKET_CLOSED and not market.is_market_open(at):
        return kite.VARIETY_AMO
    return kite.VARIETY_REGULAR


def buffered_stop_price(stop_loss: float, transaction_type: str, tick_size: float = market.DEFAULT_TICK_SIZE) -> float:
    """Applied only to the protective/stop leg of a GTT, never to a target
    leg. A GTT leg executes as a LIMIT order once triggered, so a stop placed
    at the exact trigger price can fail to fill on a gap — buffer the limit
    price slightly in the direction that makes it more likely to fill, then
    snap it onto the tick grid in that same direction.
    Long position (exit=SELL): buffer below the trigger. Short position
    (exit=BUY): buffer above."""
    buf = config.GTT_SLIPPAGE_BUFFER_PCT / 100
    if transaction_type == "BUY":  # long position, exit leg is SELL
        return market.round_to_tick(stop_loss * (1 - buf), tick_size, "down")
    return market.round_to_tick(stop_loss * (1 + buf), tick_size, "up")  # short position, exit leg is BUY


def _variety_note(kite: KiteConnect, variety: str) -> str:
    return " as AMO (market closed — queued for the next session)" if variety == kite.VARIETY_AMO else ""


def _execute_live(kite: KiteConnect, ticket: sqlite3.Row, variety: str) -> ExecutionResult:
    exchange, symbol = ticket["exchange"], ticket["symbol"]
    transaction_type, quantity, product = ticket["transaction_type"], ticket["quantity"], ticket["product_type"]
    exit_side = resolve_exit_side(transaction_type)
    tick = instruments.get_tick_size(symbol, exchange)
    stop = market.round_to_tick(ticket["stop_loss"], tick)
    target = market.round_to_tick(ticket["target_1"], tick)

    # 1. Entry order — common to both trailing and non-trailing branches. A
    # GTT alone cannot open a position, so both branches place a real order first.
    order_kwargs = dict(
        variety=variety,
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=quantity,
        product=product,
        order_type=ticket["entry_order_type"],
        tag=_order_tag(ticket),
    )
    if ticket["entry_order_type"] == "LIMIT":
        order_kwargs["price"] = market.round_to_tick(ticket["entry_price"], tick)
    # Only the broker call itself is inside the try, so every exception
    # caught here happened after the request may have left this process.
    try:
        kite_order_id = kite.place_order(**order_kwargs)
    except DEFINITE_REJECTIONS as e:
        return ExecutionResult(
            success=False, kite_order_id=None, kite_gtt_id=None,
            status="failed", event_type="execution_failed",
            message=f"Entry order rejected by Kite: {e}", order_variety=variety,
        )
    except Exception as e:
        # Ambiguous: Kite may have accepted the order before the failure. Never
        # assume either way and never retry — order_monitor resolves it.
        return unknown_result(ticket, e, variety)

    # 2. Protective exit-side GTT — placed immediately after a successful
    # entry so the position is never left naked, even briefly (and even if
    # this app is not running when the entry fills). If the entry order later
    # turns out REJECTED/CANCELLED, order_monitor.py removes this GTT again.
    try:
        last_price = kite.ltp(f"{exchange}:{symbol}")[f"{exchange}:{symbol}"]["last_price"]
        if ticket["trailing_enabled"]:
            gtt_response = kite.place_gtt(
                trigger_type=kite.GTT_TYPE_SINGLE,
                tradingsymbol=symbol,
                exchange=exchange,
                trigger_values=[stop],
                last_price=last_price,
                orders=[{
                    "transaction_type": exit_side,
                    "quantity": quantity,
                    "order_type": "LIMIT",
                    "product": product,
                    "price": buffered_stop_price(stop, transaction_type, tick),
                }],
            )
        else:
            lo, hi = sorted([stop, target])
            gtt_response = kite.place_gtt(
                trigger_type=kite.GTT_TYPE_OCO,
                tradingsymbol=symbol,
                exchange=exchange,
                trigger_values=[lo, hi],
                last_price=last_price,
                orders=[
                    {
                        "transaction_type": exit_side,
                        "quantity": quantity,
                        "order_type": "LIMIT",
                        "product": product,
                        "price": buffered_stop_price(lo, transaction_type, tick) if lo == stop else lo,
                    },
                    {
                        "transaction_type": exit_side,
                        "quantity": quantity,
                        "order_type": "LIMIT",
                        "product": product,
                        "price": buffered_stop_price(hi, transaction_type, tick) if hi == stop else hi,
                    },
                ],
            )
        kite_gtt_id = gtt_response["trigger_id"]
    except Exception as e:
        # Entry succeeded but protection failed: this is NOT a 'failed'
        # ticket — real quantity is genuinely open in the market. Surface
        # this as loudly as possible; the caller must show an unmissable
        # UI warning telling the user to place a manual stop immediately.
        return ExecutionResult(
            success=True, kite_order_id=kite_order_id, kite_gtt_id=None,
            status="executed", event_type="protection_failed",
            message=(
                f"Entry order {kite_order_id} placed{_variety_note(kite, variety)}, but the protective GTT FAILED: {e}. "
                f"UNPROTECTED POSITION — place a stop-loss in Kite manually now."
            ),
            order_variety=variety, entry_fill_status="pending",
        )

    return ExecutionResult(
        success=True, kite_order_id=kite_order_id, kite_gtt_id=kite_gtt_id,
        status="executed", event_type="executed",
        message=(
            f"order_id={kite_order_id}, gtt_id={kite_gtt_id}, "
            f"{transaction_type} {quantity} {symbol} @ {ticket['entry_order_type']}{_variety_note(kite, variety)}"
        ),
        order_variety=variety, entry_fill_status="pending",
    )


def _execute_exit(kite: KiteConnect, ticket: sqlite3.Row) -> ExecutionResult:
    """A closing ticket (is_closing_sell() — closes_ticket_id set for Phase 5
    strategy exits/Phase 8's Emergency Exit All, or source='alert_action' for
    the Quick Action popup's sell action, which reduces/exits a holding that
    may have no originating ticket at all) reduces or closes an existing
    position. It must NOT get a protective GTT of its own — there's nothing
    new to protect.

    PK-8: an accepted order id only makes the exit EXITING. The original
    position's GTT is deliberately left in place — until the exit fills it
    is still the position's only protection (an OPEN LIMIT exit, an AMO
    waiting overnight, or an exit the exchange later rejects) — and the
    position is neither closed nor journaled here. order_monitor.py
    finalizes all of that once Kite reports the full quantity COMPLETE."""
    exchange, symbol = ticket["exchange"], ticket["symbol"]
    tick = instruments.get_tick_size(symbol, exchange)
    variety = resolve_order_variety(kite)
    order_kwargs = dict(
        variety=variety,
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=ticket["transaction_type"],
        quantity=ticket["quantity"],
        product=ticket["product_type"],
        order_type=ticket["entry_order_type"],
        tag=_order_tag(ticket),
    )
    if ticket["entry_order_type"] == "LIMIT":
        order_kwargs["price"] = market.round_to_tick(ticket["entry_price"], tick)
    try:
        kite_order_id = kite.place_order(**order_kwargs)
    except DEFINITE_REJECTIONS as e:
        return ExecutionResult(
            success=False, kite_order_id=None, kite_gtt_id=None,
            status="failed", event_type="execution_failed",
            message=f"Exit order rejected by Kite: {e}", order_variety=variety,
        )
    except Exception as e:
        # Until the exit is known to exist, the position keeps everything.
        return unknown_result(ticket, e, variety)

    closes_note = f" (closes ticket {ticket['closes_ticket_id']})" if ticket["closes_ticket_id"] is not None else ""
    return ExecutionResult(
        success=True, kite_order_id=kite_order_id, kite_gtt_id=None,
        status="executed", event_type="executed",
        message=(
            f"order_id={kite_order_id}, {ticket['transaction_type']} {ticket['quantity']} {symbol} "
            f"@ {ticket['entry_order_type']}{_variety_note(kite, variety)}{closes_note} — EXITING: the exit order "
            f"is placed but not filled. The position stays open and keeps its protective GTT until Kite reports "
            f"the fill."
        ),
        order_variety=variety, entry_fill_status="pending",
    )
