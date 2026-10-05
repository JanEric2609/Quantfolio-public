"""Skfolio optimisation API endpoints."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.schemas import OptimRunRequest, OptimStressRequest
from app.foundation.auth import current_user
from app.foundation.portfolio_price_service import PortfolioPriceService
from app.foundation.settings import get_risk_free_rate

router = APIRouter(tags=["quant-optim"])


@router.post("/optim/run")
def optim_run(
    payload: OptimRunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.quant_optim import run_optimisation
    svc = PortfolioPriceService(db, user.id)
    matrix, _weights, diagnostics = svc.price_matrix()
    if not matrix:
        return {"status": "unavailable", "message": "No portfolio price data.", "diagnostics": diagnostics}
    result = run_optimisation(
        price_matrix=matrix,
        objective=payload.objective,
        risk_measure=payload.risk_measure,
        cv_scheme=payload.cv_scheme,
        lookback_days=payload.lookback_days,
        weights_constraint=payload.weights_constraint,
        risk_free_rate=get_risk_free_rate(db),
    )
    result["diagnostics"] = diagnostics
    return result


@router.post("/optim/all")
def optim_all(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.quant_optim import run_all_objectives
    svc = PortfolioPriceService(db, user.id)
    matrix, _weights, diagnostics = svc.price_matrix()
    if not matrix:
        return {"status": "unavailable", "message": "No portfolio price data.", "diagnostics": diagnostics}
    result = run_all_objectives(price_matrix=matrix)
    result["diagnostics"] = diagnostics
    return result


@router.post("/optim/stress")
def optim_stress(
    payload: OptimStressRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.quant_optim import run_stress_test
    svc = PortfolioPriceService(db, user.id)
    matrix, weights, diagnostics = svc.price_matrix()
    if not matrix:
        return {"status": "unavailable", "message": "No portfolio price data.", "diagnostics": diagnostics}
    result = run_stress_test(price_matrix=matrix, n_scenarios=payload.n_scenarios, weights=weights)
    result["diagnostics"] = diagnostics
    return result
