import sqlite3
from dataclasses import dataclass

from kiteconnect import KiteConnect
from kiteconnect.exceptions import (
    GeneralException,
    InputException,
    NetworkException,
    OrderException,
    PermissionException,
    TokenException,
)

from backend import config
from backend.db import get_connection


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
    return mode


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
    status: str  # 'executed' | 'failed'
    event_type: str  # 'executed' | 'execution_failed' | 'protection_failed'
    message: str


def execute_ticket(kite: KiteConnect, ticket: sqlite3.Row) -> ExecutionResult:
    if ticket["mode"] == "dry_run":
        return _simulate(ticket)
    if ticket["closes_ticket_id"] is not None:
        return _execute_exit(kite, ticket)
    return _execute_live(kite, ticket)


def _simulate(ticket: sqlite3.Row) -> ExecutionResult:
    return ExecutionResult(
        success=True,
        kite_order_id=None,
        kite_gtt_id=None,
        status="executed",
        event_type="executed",
        message="Simulated (dry run) — no real order placed.",
    )


def resolve_exit_side(transaction_type: str) -> str:
    return "SELL" if transaction_type == "BUY" else "BUY"


def buffered_stop_price(stop_loss: float, transaction_type: str) -> float:
    """Applied only to the protective/stop leg of a GTT, never to a target
    leg. A GTT leg executes as a LIMIT order once triggered, so a stop placed
    at the exact trigger price can fail to fill on a gap — buffer the limit
    price slightly in the direction that makes it more likely to fill.
    Long position (exit=SELL): buffer below the trigger. Short position
    (exit=BUY): buffer above."""
    buf = config.GTT_SLIPPAGE_BUFFER_PCT / 100
    if transaction_type == "BUY":  # long position, exit leg is SELL
        return stop_loss * (1 - buf)
    return stop_loss * (1 + buf)  # short position, exit leg is BUY


def _execute_live(kite: KiteConnect, ticket: sqlite3.Row) -> ExecutionResult:
    exchange, symbol = ticket["exchange"], ticket["symbol"]
    transaction_type, quantity, product = ticket["transaction_type"], ticket["quantity"], ticket["product_type"]
    exit_side = resolve_exit_side(transaction_type)

    # 1. Entry order — common to both trailing and non-trailing branches. A
    # GTT alone cannot open a position, so both branches place a real order first.
    try:
        order_kwargs = dict(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            order_type=ticket["entry_order_type"],
        )
        if ticket["entry_order_type"] == "LIMIT":
            order_kwargs["price"] = ticket["entry_price"]
        kite_order_id = kite.place_order(**order_kwargs)
    except (OrderException, InputException, GeneralException, PermissionException) as e:
        return ExecutionResult(
            success=False, kite_order_id=None, kite_gtt_id=None,
            status="failed", event_type="execution_failed",
            message=f"Entry order rejected by Kite: {e}",
        )
    except (NetworkException, TokenException) as e:
        # Ambiguous: we cannot tell if Kite actually received the order before
        # the network/auth error. Never assume success or silently retry —
        # surface this explicitly so the human verifies before resubmitting.
        return ExecutionResult(
            success=False, kite_order_id=None, kite_gtt_id=None,
            status="failed", event_type="execution_failed",
            message=(
                f"Entry order outcome UNKNOWN (network/auth error): {e}. "
                f"Verify manually in Kite before retrying — do not resubmit blindly."
            ),
        )

    # 2. Protective exit-side GTT — placed immediately after a successful
    # entry so the position is never left naked, even briefly.
    try:
        last_price = kite.ltp(f"{exchange}:{symbol}")[f"{exchange}:{symbol}"]["last_price"]
        if ticket["trailing_enabled"]:
            gtt_response = kite.place_gtt(
                trigger_type=kite.GTT_TYPE_SINGLE,
                tradingsymbol=symbol,
                exchange=exchange,
                trigger_values=[ticket["stop_loss"]],
                last_price=last_price,
                orders=[{
                    "transaction_type": exit_side,
                    "quantity": quantity,
                    "order_type": "LIMIT",
                    "product": product,
                    "price": buffered_stop_price(ticket["stop_loss"], transaction_type),
                }],
            )
        else:
            lo, hi = sorted([ticket["stop_loss"], ticket["target_1"]])
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
                        "price": buffered_stop_price(lo, transaction_type) if lo == ticket["stop_loss"] else lo,
                    },
                    {
                        "transaction_type": exit_side,
                        "quantity": quantity,
                        "order_type": "LIMIT",
                        "product": product,
                        "price": buffered_stop_price(hi, transaction_type) if hi == ticket["stop_loss"] else hi,
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
                f"Entry order {kite_order_id} placed, but the protective GTT FAILED: {e}. "
                f"UNPROTECTED POSITION — place a stop-loss in Kite manually now."
            ),
        )

    return ExecutionResult(
        success=True, kite_order_id=kite_order_id, kite_gtt_id=kite_gtt_id,
        status="executed", event_type="executed",
        message=(
            f"order_id={kite_order_id}, gtt_id={kite_gtt_id}, "
            f"{transaction_type} {quantity} {symbol} @ {ticket['entry_order_type']}"
        ),
    )


def _execute_exit(kite: KiteConnect, ticket: sqlite3.Row) -> ExecutionResult:
    """A closing ticket (closes_ticket_id set — Phase 5 strategy exits,
    Phase 8's Emergency Exit All, or any future manual close-position flow;
    ordinary manual tickets never set this field) reduces or closes an
    existing position. It must NOT get a protective GTT of its own —
    there's nothing new to protect — and the ORIGINAL position's GTT must
    be cancelled here, synchronously, once this exit fills, so a now-flat
    position doesn't keep a dangling stop-loss GTT that could later trigger
    against nothing. The original ticket's closed_at is stamped by the
    caller (routes/tickets.py's confirm_and_execute_ticket), immediately,
    in the same transaction as this exit's own execution — not left to
    trailing_engine's next scheduled tick, which would otherwise leave the
    position looking "still open" for up to TRAILING_CHECK_INTERVAL_SECONDS
    after a human just confirmed it was closed."""
    exchange, symbol = ticket["exchange"], ticket["symbol"]
    try:
        order_kwargs = dict(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=ticket["transaction_type"],
            quantity=ticket["quantity"],
            product=ticket["product_type"],
            order_type=ticket["entry_order_type"],
        )
        if ticket["entry_order_type"] == "LIMIT":
            order_kwargs["price"] = ticket["entry_price"]
        kite_order_id = kite.place_order(**order_kwargs)
    except (OrderException, InputException, GeneralException, PermissionException) as e:
        return ExecutionResult(
            success=False, kite_order_id=None, kite_gtt_id=None,
            status="failed", event_type="execution_failed",
            message=f"Exit order rejected by Kite: {e}",
        )
    except (NetworkException, TokenException) as e:
        return ExecutionResult(
            success=False, kite_order_id=None, kite_gtt_id=None,
            status="failed", event_type="execution_failed",
            message=(
                f"Exit order outcome UNKNOWN (network/auth error): {e}. "
                f"Verify manually in Kite before retrying — do not resubmit blindly."
            ),
        )

    conn = get_connection()
    try:
        original = conn.execute(
            "SELECT kite_gtt_id FROM trade_tickets WHERE id = ?", (ticket["closes_ticket_id"],)
        ).fetchone()
    finally:
        conn.close()

    gtt_note = ""
    if original is not None and original["kite_gtt_id"] is not None:
        try:
            kite.delete_gtt(original["kite_gtt_id"])
        except Exception as e:
            gtt_note = (
                f" WARNING: exit order filled, but deleting the original position's GTT "
                f"({original['kite_gtt_id']}) FAILED: {e}. Remove it manually in Kite — "
                f"it is now orphaned against a closed/reduced position."
            )

    return ExecutionResult(
        success=True, kite_order_id=kite_order_id, kite_gtt_id=None,
        status="executed", event_type="executed",
        message=(
            f"order_id={kite_order_id}, {ticket['transaction_type']} {ticket['quantity']} {symbol} "
            f"@ {ticket['entry_order_type']} (closes ticket {ticket['closes_ticket_id']})"
        ) + gtt_note,
    )
