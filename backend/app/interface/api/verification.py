"""Portfolio risk API endpoints (Portfolio -> Risk).

GET /api/verification/risk/{portfolio_id}   — VaR, drawdown, concentration, currency look-through, alerts
GET /api/verification/stress/{portfolio_id} — what-if scenarios (market drop, FX)

``portfolio_id`` may be the literal ``main`` for the signed-in user's main
portfolio, so the page does not need a separate lookup.

The confidence score, attribution, comparison, report, summary and audit
endpoints that lived here were removed: the score was never compared to an
outcome and the return series behind the rest counted deposits as returns. The
"was the system right?" question is answered by ``/api/trust``.
"""

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.decision.verification.risk import risk_assessment_with_alerts
from app.decision.verification.stress import run_stress_test
from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.models.entities import Portfolio, User
from app.foundation.portfolio_service import main_portfolio
from app.foundation.schemas import RiskAssessmentOut, StressTestResultOut

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/verification",
    tags=["verification"],
)


def _resolve_portfolio(portfolio_id: str, user: User, db: Session) -> Portfolio:
    """The user's portfolio for *portfolio_id* (``main`` = main portfolio); 404 otherwise."""
    if portfolio_id == "main":
        return main_portfolio(db, user.id)
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None or portfolio.user_id != user.id:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


@router.get("/risk/{portfolio_id}", response_model_exclude_unset=True, response_model=RiskAssessmentOut)
def get_risk_assessment(
    portfolio_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Risk assessment for a portfolio.

    VaR, drawdown and Sharpe/Sortino come from the daily returns of the
    current holdings (price history), never from account balances. Also
    returns concentration (holding-level and look-through), asset-type and
    currency exposure (index ETFs looked through to their constituents'
    currencies), and the alerts the Risk page shows.
    """
    portfolio = _resolve_portfolio(portfolio_id, user, db)
    assessment, alerts = risk_assessment_with_alerts(portfolio.id, db)
    result = asdict(assessment)
    result["alerts"] = [asdict(a) for a in alerts]
    return result


@router.get("/stress/{portfolio_id}", response_model_exclude_unset=True, response_model=StressTestResultOut)
def get_stress_test(
    portfolio_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """What-if scenarios on today's holdings: a market drop and two FX scenarios.

    Illustrations of exposure, not forecasts — no probabilities are attached.
    """
    portfolio = _resolve_portfolio(portfolio_id, user, db)
    return asdict(run_stress_test(portfolio.id, db))
