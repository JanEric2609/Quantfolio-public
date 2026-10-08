"""Monthly plan API: "what do I do with this month's contribution?".

Wraps :func:`app.decision.monthly_plan.build_monthly_plan`; see that module
for the allocation rules (contribution-first, sells only past the drift
band, sleeves unlock only on evidence). The month view itself is read-only;
``POST /actions/snapshot`` stores its actions so a later sync can link a
placed order back to the plan action it fulfils (ADR 0019 §3).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.decision.monthly_plan import build_monthly_plan
from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.models.entities import MonthlyPlanAction, User

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
    dynamization_rate: float | None = None
    running: bool = True
    not_running_reason: str | None = None  # paused | overdue


class RunningSavingsPlans(BaseModel):
    items: list[RunningSavingsPlan] = []
    stopped: list[RunningSavingsPlan] = []
    monthly_eur: float = 0.0
    by_sleeve: dict[str, float] = {}
    # When the plans were last really fetched, and whether the latest fetch failed.
    synced_at: str | None = None
    last_fetch_failed: bool = False
    # Running plans into locked sleeves (e.g. stock picks), taken out of the contribution.
    other_budget_eur: float = 0.0
    other_budget_pct: float = 0.0
    core_needed_eur: float | None = None


class EvidenceGateFreshness(BaseModel):
    computed_at: str | None = None
    as_of: str | None = None
    n_trials: int
    stale: bool = False


class TiltEvidence(BaseModel):
    passed: bool
    unlocked: bool
    reason: str = ""


class SatelliteEvidence(BaseModel):
    unlocked: bool
    reason: str = ""
    gate: EvidenceGateFreshness | None = None


class PlanEvidence(BaseModel):
    tilt: TiltEvidence
    satellite: SatelliteEvidence


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


class SuggestedFund(BaseModel):
    sleeve: str
    isin: str | None = None
    name: str
    why: str
    overridden: bool = False


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
    evidence: PlanEvidence | None = None
    cash: PlanCash | None = None
    core_look_through: CoreLookThrough | None = None
    notes: list[str] = []
    never_sells: bool
    not_investment_advice: bool
    suggested_funds: list[SuggestedFund] = []
    acc_dist_note: str = ""


@router.get("/month", response_model=MonthlyPlanResponse)
def month_plan(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> MonthlyPlanResponse:
    """This month's plan: how to split the contribution, and whether anything else changes."""
    return MonthlyPlanResponse.model_validate(build_monthly_plan(db, user.id))


class StoredPlanAction(BaseModel):
    id: str
    month: str
    sleeve: str
    kind: str
    amount_eur: float
    instrument: str
    ticker: str | None = None
    isin: str | None = None
    broker: str | None = None
    account_id: str | None = None
    status: str
    fulfilled_at: str | None = None
    note: str = ""


@router.post("/actions/snapshot", response_model=list[StoredPlanAction])
def snapshot_actions(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[StoredPlanAction]:
    """Store this month's computed plan actions (idempotent per month).

    A placed order the next sync finds is linked back to its action, so the
    Decide page can show which plan action a trade fulfils (ADR 0019 §3).
    """
    from app.decision.monthly_plan_store import serialize, snapshot_monthly_plan

    return [StoredPlanAction(**serialize(row)) for row in snapshot_monthly_plan(db, user.id)]


@router.get("/actions", response_model=list[StoredPlanAction])
def list_actions(
    month: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[StoredPlanAction]:
    """Stored plan actions, newest month first, optionally filtered to one month."""
    from app.decision.monthly_plan_store import serialize

    query = db.query(MonthlyPlanAction).filter(MonthlyPlanAction.user_id == user.id)
    if month:
        query = query.filter(MonthlyPlanAction.month == month)
    query = query.order_by(MonthlyPlanAction.month.desc(), MonthlyPlanAction.sleeve)
    return [StoredPlanAction(**serialize(row)) for row in query.all()]
