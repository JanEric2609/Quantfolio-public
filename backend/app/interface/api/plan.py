"""Monthly plan API: "what do I do with this month's contribution?".

Read-only. Wraps :func:`app.decision.monthly_plan.build_monthly_plan`; see that
module for the allocation rules (contribution-first, sells only past the drift
band, sleeves unlock only on evidence).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.decision.monthly_plan import build_monthly_plan
from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.models.entities import User

router = APIRouter(prefix="/api/plan", tags=["plan"])


class PlanPosition(BaseModel):
    isin: str
    ticker: str | None = None
    name: str
    value_eur: float


class PlanSleeve(BaseModel):
    key: str
    label: str
    current_eur: float
    current_pct: float
    target_pct: float
    max_pct: float
    unlocked: bool
    status: str
    contribution_eur: float
    after_eur: float
    after_pct: float
    positions: list[PlanPosition]
    sale_eur: float = 0.0
    reinvest_eur: float = 0.0


class PlanAction(BaseModel):
    sleeve: str
    kind: str  # savings_plan | order | sale | one_off (optional, from cash above the reserve)
    amount_eur: float
    instrument: str
    ticker: str | None = None
    isin: str | None = None
    note: str
    broker: str | None = None
    broker_label: str | None = None
    account_id: str | None = None


class RunningSavingsPlan(BaseModel):
    broker: str
    broker_label: str
    name: str
    isin: str | None = None
    sleeve: str
    amount_eur: float
    frequency: str
    monthly_eur: float | None = None
    next_execution_date: str | None = None


class RunningSavingsPlans(BaseModel):
    items: list[RunningSavingsPlan] = []
    monthly_eur: float = 0.0
    by_sleeve: dict[str, float] = {}


class PlanCash(BaseModel):
    savings_eur: float
    broker_cash_eur: float
    broker_cash_by_broker: dict[str, float] = {}
    emergency_reserve_eur: float
    reserve_set: bool
    investable_eur: float


class CoreLookThrough(BaseModel):
    em_eur: float
    # None when no core fund has a known split (all of it is unknown_eur).
    em_pct: float | None
    world_em_pct: float
    unknown_eur: float = 0.0


class TrackingErrorBudget(BaseModel):
    label: str
    tilt_max_pct: float
    satellite_max_pct: float


class MonthlyPlanResponse(BaseModel):
    month: str
    generated_at: str
    headline: str
    no_change: bool
    contribution_eur: float
    book_eur: float
    holdings_synced_at: str | None = None
    has_holdings: bool
    tracking_error_budget: TrackingErrorBudget
    min_order_eur: float
    drift_band_pp: float | None = None
    contribution_mode: str = "savings_plan"
    broker: str = "dkb"
    broker_label: str = "DKB"
    broker_choice: str = "auto"
    brokers_connected: list[str] = []
    sleeves: list[PlanSleeve]
    actions: list[PlanAction]
    savings_plans: RunningSavingsPlans | None = None
    cash: PlanCash | None = None
    core_look_through: CoreLookThrough | None = None
    notes: list[str] = []
    never_sells: bool
    not_investment_advice: bool


@router.get("/month", response_model=MonthlyPlanResponse)
def month_plan(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> MonthlyPlanResponse:
    """This month's plan: how to split the contribution, and whether anything else changes."""
    return MonthlyPlanResponse.model_validate(build_monthly_plan(db, user.id))
