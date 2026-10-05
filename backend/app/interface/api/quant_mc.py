"""Monte Carlo simulation API endpoints (Phase 2)."""

import json
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import McRun, User
from app.foundation.auth import current_user
from app.foundation.quant_mc import MonteCarloEngine, McSpec
from app.interface.api.safe_endpoint import safe_endpoint

router = APIRouter(prefix="/api/quant/mc", tags=["quant-mc"])


# Request/Response Schemas
class McSpecSchema(BaseModel):
    """Request schema for MC runs."""

    model: Literal["gbm_euler", "gbm_milstein", "heston_euler", "ou_euler"] = "gbm_euler"
    model_params: Optional[dict] = None
    horizon_steps: int = 100
    T: float = 1.0
    n_paths: int = 10000
    payoff: Literal["european_call", "european_put", "asian_call", "lookback_call", "barrier_call", "portfolio_var", "portfolio_es"] = "european_call"
    payoff_params: Optional[dict] = None
    distribution: Literal["normal", "student_t", "nig"] = "normal"
    distribution_params: Optional[dict] = None
    variance_reduction: Optional[list[str]] = None
    # Optional caller-supplied data for the variance-reduction reducers
    # (control_values, log_likelihood_ratio, stratum_indices). The engine
    # self-generates these when omitted for the techniques it knows how to
    # (see MonteCarloEngine.run); this field exists so a supplied override is
    # never silently dropped at the API boundary (deepdive-03-quantlab Issue 2).
    additional_data: Optional[dict] = None
    use_mlmc: bool = False
    mlmc_levels: Optional[int] = None
    seed: Optional[int] = None
    return_paths: bool = False
    compute_greeks: bool = True
    compute_greeks_method: Literal["pathwise", "likelihood_ratio"] = "pathwise"

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "model": "gbm_euler",
                "model_params": {"S0": 100.0, "mu": 0.05, "sigma": 0.2},
                "horizon_steps": 100,
                "T": 1.0,
                "n_paths": 10000,
                "payoff": "european_call",
                "payoff_params": {"strike": 100.0},
                "distribution": "normal",
                "variance_reduction": ["antithetic"],
            }
        }
    )


class McResultSchema(BaseModel):
    """Response schema for MC results."""

    research: dict  # Contains estimate, stderr, greeks, etc.
    attribution: Optional[dict] = None
    ledger_ref: Optional[str] = None


class McRunDetailSchema(BaseModel):
    """Detail schema for a stored MC run."""

    id: UUID
    user_id: UUID
    spec: dict
    results: dict
    paths_summary: Optional[dict] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def model_validate(cls, obj, **kwargs):  # type: ignore[override]
        """Deserialise JSON string columns from the ORM model before validation."""
        if hasattr(obj, "spec_json"):
            data = {
                "id": obj.id,
                "user_id": obj.user_id,
                "spec": json.loads(obj.spec_json) if obj.spec_json else {},
                "results": json.loads(obj.results_json) if obj.results_json else {},
                "paths_summary": json.loads(obj.paths_summary_json) if obj.paths_summary_json else None,
                "created_at": obj.created_at,
            }
            return super().model_validate(data, **kwargs)
        return super().model_validate(obj, **kwargs)


@router.post("/run", response_model=McResultSchema)
@safe_endpoint
def run_monte_carlo(
    spec_request: McSpecSchema,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Execute a Monte Carlo simulation.

    Returns three-artifact envelope: research, attribution (null), ledger_ref (null).
    """
    # Convert request to McSpec
    spec = McSpec(
        model=spec_request.model,
        model_params=spec_request.model_params or {},
        horizon_steps=spec_request.horizon_steps,
        T=spec_request.T,
        n_paths=spec_request.n_paths,
        payoff=spec_request.payoff,
        payoff_params=spec_request.payoff_params or {},
        distribution=spec_request.distribution,
        distribution_params=spec_request.distribution_params or {},
        variance_reduction=spec_request.variance_reduction or [],
        additional_data=spec_request.additional_data,
        use_mlmc=spec_request.use_mlmc,
        mlmc_levels=spec_request.mlmc_levels,
        seed=spec_request.seed,
        return_paths=spec_request.return_paths,
        compute_greeks=spec_request.compute_greeks,
        compute_greeks_method=spec_request.compute_greeks_method,
    )

    # Execute simulation
    engine = MonteCarloEngine()
    result, downsampled_paths = engine.run(spec)

    # Package as three-artifact envelope
    research = {
        "estimate": result.estimate,
        "stderr": result.stderr,
        "n_effective": result.n_effective,
        "variance_reduction_gain": result.variance_reduction_gain,
        "greeks": result.greeks,
        "percentiles": result.percentiles,
        "paths_summary": result.paths_summary,
        "mlmc_stats": result.mlmc_stats,
    }

    # Persist the run so GET /run/{run_id} can look it up.
    run_id = str(uuid4())
    mc_run = McRun(
        id=run_id,
        user_id=str(user.id),
        spec_json=spec.to_json(),
        results_json=json.dumps(research, default=str),
        paths_summary_json=json.dumps(result.paths_summary, default=str) if result.paths_summary else None,
    )
    db.add(mc_run)
    db.commit()

    return {
        "research": research,
        "attribution": None,
        "ledger_ref": run_id,
    }


@router.get("/run/{run_id}", response_model=McRunDetailSchema)
def get_mc_run(
    run_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> McRun:
    """Retrieve a stored MC run by ID."""
    mc_run = db.query(McRun).filter(
        McRun.id == run_id,
        McRun.user_id == user.id,
    ).first()

    if not mc_run:
        raise HTTPException(status_code=404, detail="MC run not found")

    return mc_run


@router.get("/runs", response_model=list[McRunDetailSchema])
def list_mc_runs(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[McRun]:
    """List recent MC runs for the current user."""
    runs = db.query(McRun).filter(
        McRun.user_id == user.id,
    ).order_by(
        McRun.created_at.desc(),
    ).offset(offset).limit(limit).all()

    return runs
