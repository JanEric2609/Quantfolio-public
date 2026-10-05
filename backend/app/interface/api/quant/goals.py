"""Goals API endpoints — goal-based portfolio construction, Monte Carlo, contributions, rebalancing, lifecycle."""

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.interface.api.quant._common import book_volatility
from app.foundation.core.db import get_db
from app.foundation.models.entities import Goal, User
from app.foundation.auth import current_user

router = APIRouter(tags=["quant-goals"])


@router.get("/goals/{goal_id}/portfolio")
def goal_portfolio(
    goal_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.goal_optimizer import build_goal_portfolio
    return build_goal_portfolio(db, user.id, goal_id)


def _required_real_return(start: float, monthly: float, years: int, target: float) -> float:
    """Annual return at which start + monthly payments (at the start of each month) reach target exactly."""
    from scipy.optimize import brentq

    months = years * 12
    if target <= 0 or months <= 0:
        return 0.0

    def gap(annual: float) -> float:
        m = (1.0 + annual) ** (1.0 / 12.0) - 1.0
        growth = (1.0 + m) ** months
        annuity = monthly * months if abs(m) < 1e-12 else monthly * (growth - 1.0) / m * (1.0 + m)
        return start * growth + annuity - target

    try:
        return float(cast(float, brentq(gap, -0.99, 5.0)))
    except ValueError:  # already there at -99 %, or out of reach even at +500 %
        return -0.99 if gap(-0.99) > 0 else 5.0


@router.post("/goals/{goal_id}/mc")
def goal_monte_carlo(
    goal_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Chance of reaching a goal, from the same planner as the Quant Lab projection.

    The goal's own monthly contribution is paid in every month (the old version
    simulated the starting amount alone, at the historical mean as drift), the
    drift is the published long-run real return from the settings and the
    volatility is the book's. Amounts are treated as today's euros.
    """
    from datetime import date

    from app.foundation.goal_optimizer import calculate_savings_trajectory
    from app.foundation.settings import get_public_settings
    from app.foundation.wealth_planner import DEFAULT_CMA_REAL_RETURN, PlanInputs, project

    goal = db.query(Goal).filter(Goal.id == goal_id, Goal.user_id == user.id).first()
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")

    start_value = float(goal.progress or 0)
    target = float(goal.target_amount or 0)
    monthly = float(goal.monthly_contribution or 0)
    years = 10
    if goal.target_date:
        years = max(1, round((goal.target_date - date.today()).days / 365.0))
    volatility, vol_source = book_volatility(db, user.id)
    real_return = float(get_public_settings(db).get("mc_cma_real_return", DEFAULT_CMA_REAL_RETURN))
    result = project(PlanInputs(
        start_value=start_value, monthly_contribution=monthly, years=years, real_return=real_return,
        volatility=volatility, goal=target if target > 0 else None, paths=5_000,
    ))
    fan = result["fan"]
    fan_chart = [
        {"step": f["year"], "p05": f["p5"], "p25": f["p25"], "median": f["p50"], "p75": f["p75"], "p95": f["p95"]}
        for f in fan
    ]
    goal_trajectory = [
        {"step": f["year"], "value": start_value + (target - start_value) * f["year"] / max(years, 1)} for f in fan
    ]
    trajectory = calculate_savings_trajectory(goal)
    probability = (result.get("goal") or {}).get("probability")
    return {
        "status": "completed",
        "fan_chart": fan_chart,
        "goal_trajectory": goal_trajectory,
        "on_track": result["terminal"]["median"] >= target if target > 0 else False,
        "probability": probability,
        "goal": result.get("goal"),
        "required_return": round(_required_real_return(start_value, monthly, years, target), 4),
        "median_path": [f["p50"] for f in fan],
        "assumptions": {
            "real_return": real_return,
            "annual_return": real_return,  # kept for older clients; a geometric real return now
            "annual_volatility": volatility,
            "volatility_source": vol_source,
            "years": years,
            "start_value": start_value,
            "monthly_contribution": monthly,
            "simulations": result["inputs"]["paths"],
            "real_terms": True,
        },
        "trajectory": {
            "months_remaining": trajectory.get("months_remaining", years * 12),
            "required_monthly": str(trajectory.get("required_monthly", "0")),
            "projected_amount": str(trajectory.get("projected_amount", "0")),
        },
        "estimate": True,
    }


@router.get("/goals")
def list_goals(limit: int = 200, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    goals = db.query(Goal).filter(Goal.user_id == user.id).order_by(Goal.created_at.desc()).limit(max(1, min(limit, 1000))).all()
    return [
        {
            "id": g.id,
            "title": g.title,
            "target_amount": str(g.target_amount) if g.target_amount is not None else None,
            "target_date": g.target_date.isoformat() if g.target_date else None,
            "progress": str(g.progress) if g.progress is not None else None,
            "risk_tolerance": g.risk_tolerance,
            "asset_class_targets": g.asset_class_targets,
            "monthly_contribution": str(g.monthly_contribution) if g.monthly_contribution is not None else None,
            "notes": g.notes,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }
        for g in goals
    ]


@router.get("/goals/contribution")
def goal_contribution(
    monthly_income: float | None = Query(None, description="Monthly net income"),
    monthly_expenses: float | None = Query(None, description="Monthly fixed expenses"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.goal_optimizer import contribution_optimizer

    return contribution_optimizer(
        db, user.id,
        monthly_income=monthly_income,
        monthly_expenses=monthly_expenses,
    )


@router.get("/goals/rebalance")
def goal_rebalance(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.goal_optimizer import rebalance_with_goals
    return rebalance_with_goals(db, user.id)
