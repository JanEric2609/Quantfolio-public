"""Lab overview API endpoint — unified cockpit."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.interface.api.quant._common import _portfolio_returns_series
from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.portfolio_price_service import PortfolioPriceService
from app.foundation.quant import portfolio_risk_from_price_matrix, unavailable_portfolio
from app.foundation import quant_metrics

router = APIRouter(tags=["quant-lab"])


@router.get("/lab/overview")
def lab_overview(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    """Unified cockpit combining V1 /cockpit + V2 /portfolio/summary."""
    svc = PortfolioPriceService(db, user.id)
    matrix, weights, diagnostics = svc.price_matrix()
    risk = portfolio_risk_from_price_matrix(matrix, weights) if matrix else unavailable_portfolio()
    returns, _ = _portfolio_returns_series(matrix, weights) if matrix else ([], [])
    summary = quant_metrics.full_risk_report(returns) if returns else {}
    return {
        "status": "completed" if matrix else "needs_data",
        "diagnostics": diagnostics,
        "risk_legacy": risk,
        "risk_summary": summary,
        "available_modules": [
            "overview", "risk", "optim", "backtests", "factors",
            "ml", "rl", "qlib", "experiments", "graveyard", "data_health",
        ],
    }
