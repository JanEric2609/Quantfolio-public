"""Risk analytics API endpoints — VaR, factor-model, correlation, Monte Carlo, efficient frontier, portfolio backtest."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.interface.api.quant._common import _portfolio_price_matrix, compute_factor_model
from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.portfolio_price_service import PortfolioPriceService
from app.foundation.quant import (
    correlation_matrix_from_price_matrix,
    historical_var,
    parametric_var,
    portfolio_risk_from_price_matrix,
    price_matrix_to_returns,
    unavailable_portfolio,
)

router = APIRouter(tags=["quant-risk"])
logger = logging.getLogger(__name__)


@router.get("/factor-model")
def factor_model(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    return compute_factor_model(db, user.id)


@router.get("/cockpit")
def quant_cockpit(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    svc = PortfolioPriceService(db, user.id)
    matrix, weights, diagnostics = svc.price_matrix()
    risk = portfolio_risk_from_price_matrix(matrix, weights) if matrix else unavailable_portfolio()
    factors = factor_model(db, user)
    return {
        "status": "completed" if matrix else "needs_data",
        "diagnostics": diagnostics,
        "risk": risk,
        "factor_model": factors,
        "available_modules": ["factor_model", "risk", "var", "correlation", "frontier", "experiments"],
    }


@router.get("/correlation-matrix")
def correlation_matrix(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    matrix, _weights = _portfolio_price_matrix(db, user.id)
    return correlation_matrix_from_price_matrix(matrix)


@router.get("/correlation/rolling")
def rolling_correlation(
    a: str,
    b: str,
    days: int = Query(default=60, ge=5, le=520),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    matrix, _weights = _portfolio_price_matrix(db, user.id)
    if a not in matrix or b not in matrix:
        raise HTTPException(status_code=404, detail="Portfolio symbol not found")
    try:
        returns = price_matrix_to_returns({a: matrix[a], b: matrix[b]})
        series = returns[a].rolling(days).corr(returns[b]).dropna().tail(520)
        # returns' index is now a genuine DatetimeIndex (price_matrix_to_returns
        # normalizes it); format explicitly as a date-only string to preserve
        # this endpoint's prior "YYYY-MM-DD" wire format (str(Timestamp) would
        # otherwise append a spurious " 00:00:00" time component).
        return {
            "a": a,
            "b": b,
            "days": days,
            "timeline": [
                {"date": index.strftime("%Y-%m-%d"), "value": float(value)}
                for index, value in series.items()
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Unable to compute rolling correlation: {exc}") from exc


@router.get("/var")
def value_at_risk(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    matrix, weights = _portfolio_price_matrix(db, user.id)
    if matrix:
        try:
            import pandas as pd

            returns_frame = price_matrix_to_returns(matrix)
            weight_series = pd.Series(weights).reindex(returns_frame.columns).fillna(0)
            weight_series = weight_series / weight_series.sum() if weight_series.sum() else weight_series
            returns = returns_frame.mul(weight_series, axis=1).sum(axis=1).tolist()
        except Exception:
            logger.warning("VaR returns computation failed; reporting unavailable", exc_info=True)
            returns = []
    else:
        returns = []
    if not returns:
        return {"status": "unavailable", "message": "No portfolio history available."}
    return {
        "historical_95": historical_var(returns, 0.95),
        "historical_99": historical_var(returns, 0.99),
        "parametric_95": parametric_var(returns, 0.95),
        "parametric_99": parametric_var(returns, 0.99),
    }


# POST /monte-carlo (historical mean as drift, one year) and GET
# /efficient-frontier (mean-variance on historical means) were removed on
# 2026-10-04: nothing called them, and GET /portfolio/projection and GET
# /portfolio/optimization replace them with a long-run drift and
# covariance-only targets.
