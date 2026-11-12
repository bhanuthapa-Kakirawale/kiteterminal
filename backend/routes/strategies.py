from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect
from pydantic import ValidationError

from backend import backtest, instruments, risk_engine, strategy_engine
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.models import STRATEGY_PARAMS_MODELS, BacktestRequest, StrategyConfigUpdateRequest
from backend.strategies import STRATEGIES
from backend.ticker import ticker_manager
from backend.watchlist import list_watchlist
from backend.instance_lock import WRITE_AUTHORITY

router = APIRouter()


def _require_known_strategy(strategy_name: str) -> None:
    if strategy_name not in STRATEGIES:
        raise HTTPException(404, {
            "error_code": "UNKNOWN_STRATEGY",
            "message": f"'{strategy_name}' is not a registered strategy. Known: {list(STRATEGIES.keys())}",
        })


def _validate_params(strategy_name: str, params: dict) -> dict:
    model = STRATEGY_PARAMS_MODELS[strategy_name]
    try:
        return model(**params).model_dump()
    except ValidationError as e:
        raise HTTPException(400, {"error_code": "VALIDATION_ERROR", "message": str(e)})


# NOTE: every fixed-literal-path route below (scan-now, stop-adjustments,
# backtests) MUST be registered BEFORE the generic /api/strategies/{strategy_name}
# routes further down — FastAPI/Starlette matches routes in registration
# order, and {strategy_name} would otherwise greedily swallow "backtests",
# "scan-now" etc. as a literal strategy_name and 404 before reaching the
# real handler.

@router.get("/api/strategies")
def list_strategies(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        return {"data": strategy_engine.list_all_configs(conn, account["id"])}
    finally:
        conn.close()


@router.post("/api/strategies/scan-now", dependencies=WRITE_AUTHORITY)
def scan_now(kite: KiteConnect = Depends(require_trading_account)):
    # Manual trigger for the same entry -> exit -> breakeven chain the daily
    # job runs — now covers every strategy_configs row with enabled=True in
    # one call, matching the nightly job's own loop (Phase 7 Part A.3).
    account = get_trading_account()
    try:
        result = strategy_engine.run_daily_scans(account, kite)
    except risk_engine.RiskCheckUnavailableError as e:
        raise HTTPException(status_code=e.status_code, detail={"error_code": e.error_code, "message": e.message})
    ticker_manager.broadcast({"type": "tickets_changed", "reason": "scan"})
    return {"data": result}


@router.get("/api/strategies/stop-adjustments")
def list_stop_adjustments(status: str = "pending", kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT sap.*, t.symbol, t.quantity, t.transaction_type, t.entry_price, t.mode
            FROM stop_adjustment_proposals sap
            JOIN trade_tickets t ON t.id = sap.ticket_id
            WHERE t.account_id = ? AND sap.status = ?
            ORDER BY sap.created_at DESC
            """,
            (account["id"], status),
        ).fetchall()
        return {"data": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/api/strategies/stop-adjustments/{proposal_id}/confirm", dependencies=WRITE_AUTHORITY)
def confirm_stop_adjustment(proposal_id: int, kite: KiteConnect = Depends(require_trading_account)):
    conn = get_connection()
    try:
        result = strategy_engine.resolve_stop_adjustment(conn, kite, proposal_id, confirm=True)
        ticker_manager.broadcast({"type": "tickets_changed", "reason": "stop_adjustment"})
        return {"data": result}
    except ValueError as e:
        raise HTTPException(status_code=409, detail={"error_code": "CONFLICT_INVALID_STATE", "message": str(e)})
    finally:
        conn.close()


@router.post("/api/strategies/stop-adjustments/{proposal_id}/reject", dependencies=WRITE_AUTHORITY)
def reject_stop_adjustment(proposal_id: int, kite: KiteConnect = Depends(require_trading_account)):
    conn = get_connection()
    try:
        result = strategy_engine.resolve_stop_adjustment(conn, kite, proposal_id, confirm=False)
        ticker_manager.broadcast({"type": "tickets_changed", "reason": "stop_adjustment"})
        return {"data": result}
    except ValueError as e:
        raise HTTPException(status_code=409, detail={"error_code": "CONFLICT_INVALID_STATE", "message": str(e)})
    finally:
        conn.close()


@router.post("/api/strategies/backtests", dependencies=WRITE_AUTHORITY)
def run_backtest_endpoint(req: BacktestRequest, kite: KiteConnect = Depends(require_trading_account)):
    _require_known_strategy(req.strategy_name)
    if req.start_date > req.end_date:
        raise HTTPException(400, {"error_code": "INVALID_DATE_RANGE", "message": "start_date must not be after end_date"})
    account = get_trading_account()
    conn = get_connection()
    try:
        universe_source = "user_supplied" if req.symbols else "current_watchlist"
        if req.symbols:
            pairs = []
            for sym in req.symbols:
                inst = instruments.get_instrument_by_symbol(sym, "NSE")
                if inst is None:
                    raise HTTPException(400, {
                        "error_code": "INSTRUMENT_NOT_FOUND", "message": f"Could not resolve '{sym}' on NSE",
                    })
                pairs.append((inst["instrument_token"], sym))
        else:
            pairs = [(w["instrument_token"], w["symbol"]) for w in list_watchlist(account["id"])]

        if not pairs:
            raise HTTPException(400, {
                "error_code": "NO_SYMBOLS",
                "message": "No symbols to backtest — add symbols to the watchlist or pass symbols explicitly.",
            })

        if req.params:
            params = _validate_params(req.strategy_name, req.params)
        else:
            params = strategy_engine.get_config(conn, account["id"], req.strategy_name)["params"]

        # PK-21: the backtest sizes against its own simulated capital, so it
        # no longer reads the live portfolio (no broker call is made).
        try:
            cfg = backtest.BacktestConfig.from_overrides(
                initial_capital=req.initial_capital, risk_per_trade_pct=req.risk_per_trade_pct,
                capital_per_trade_pct=req.capital_per_trade_pct, max_open_positions=req.max_open_positions,
                entry_slippage_bps=req.entry_slippage_bps, exit_slippage_bps=req.exit_slippage_bps,
                costs_enabled=req.costs_enabled, entry_model=req.entry_model,
                simulate_breakeven=req.simulate_breakeven,
            )
        except ValueError as e:
            raise HTTPException(400, {"error_code": "INVALID_BACKTEST_CONFIG", "message": str(e)})
        result = backtest.run_backtest(
            conn, req.strategy_name, pairs, params, req.start_date, req.end_date, cfg=cfg,
            universe_source=universe_source,
            run_walk_forward=req.run_walk_forward, window_days=req.window_days,
            run_monte_carlo=req.run_monte_carlo, mc_iterations=req.mc_iterations, mc_random_seed=req.mc_random_seed,
        )
        return {"data": result}
    finally:
        conn.close()


@router.get("/api/strategies/backtests")
def list_backtests_endpoint(kite: KiteConnect = Depends(require_trading_account)):
    conn = get_connection()
    try:
        return {"data": backtest.list_backtests(conn)}
    finally:
        conn.close()


@router.get("/api/strategies/backtests/{run_id}")
def get_backtest_endpoint(run_id: int, kite: KiteConnect = Depends(require_trading_account)):
    conn = get_connection()
    try:
        data = backtest.get_backtest(conn, run_id)
        if data is None:
            raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "Backtest run not found"})
        return {"data": data}
    finally:
        conn.close()


# --- Generic per-strategy config routes: MUST come after every fixed path above. ---

@router.get("/api/strategies/{strategy_name}")
def get_strategy_config(strategy_name: str, kite: KiteConnect = Depends(require_trading_account)):
    _require_known_strategy(strategy_name)
    account = get_trading_account()
    conn = get_connection()
    try:
        return {"data": strategy_engine.get_config(conn, account["id"], strategy_name)}
    finally:
        conn.close()


@router.put("/api/strategies/{strategy_name}", dependencies=WRITE_AUTHORITY)
def save_strategy_config(
    strategy_name: str, req: StrategyConfigUpdateRequest, kite: KiteConnect = Depends(require_trading_account)
):
    _require_known_strategy(strategy_name)
    validated_params = _validate_params(strategy_name, req.params)
    account = get_trading_account()
    conn = get_connection()
    try:
        data = strategy_engine.save_config(conn, account["id"], strategy_name, req.enabled, validated_params)
        return {"data": data}
    finally:
        conn.close()


@router.get("/api/strategies/{strategy_name}/versions")
def get_strategy_versions(strategy_name: str, kite: KiteConnect = Depends(require_trading_account)):
    _require_known_strategy(strategy_name)
    account = get_trading_account()
    conn = get_connection()
    try:
        return {"data": strategy_engine.get_version_history(conn, account["id"], strategy_name)}
    finally:
        conn.close()
