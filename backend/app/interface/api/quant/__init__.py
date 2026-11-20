"""Quant API package — domain-split route modules.

The main router carries the ``/api/quant`` prefix.  Each sub-module registers
its routes without a prefix so that FastAPI concatenates the parent prefix.

Backward compatibility: commonly-imported symbols are re-exported so existing
callers (tests) continue to work.
"""

from fastapi import APIRouter

from app.interface.api.quant import (
    backtest,
    data,
    experiments,
    factors,
    goals,
    lab,
    market,
    optim,
    portfolio,
    regime,
    research,
    risk,
)

# Re-export commonly-used symbols for backward compatibility
from app.interface.api.quant.experiments import list_recent_runs  # noqa: F401
from app.interface.api.quant.goals import goal_monte_carlo  # noqa: F401

router = APIRouter(prefix="/api/quant", tags=["quant"])

router.include_router(backtest.router)
router.include_router(data.router)
router.include_router(experiments.router)
router.include_router(factors.router)
router.include_router(goals.router)
router.include_router(lab.router)
router.include_router(market.router)
router.include_router(optim.router)
router.include_router(portfolio.router)
router.include_router(regime.router)
router.include_router(research.router)
router.include_router(risk.router)
