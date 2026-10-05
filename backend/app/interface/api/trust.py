"""Trust API — "Can I trust it?" (frozen-call verdicts).

GET /api/trust/verdict — headline, three numbers and one evidence row per prediction type
GET /api/trust/calls   — resolved calls, newest first, with the five worst misses

Both read the immutable prediction ledgers for the signed-in user only. Every
value is frozen at issue/resolution time; see ``decision/verification/trust.py``
for the definitions.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.decision.verification.trust import build_verdict, list_calls
from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.models.entities import User

router = APIRouter(prefix="/api/trust", tags=["trust"])

EvidenceState = Literal["too_early", "skill", "harm", "no_evidence"]
TypeKey = Literal["ideas", "advisor", "mandates", "regime"]


class SkillOut(BaseModel):
    metric: str
    value: float
    ci_low: float | None = None
    ci_high: float | None = None
    n: int = Field(description="Rebalance dates (the independent unit).")
    n_calls: int = 0
    benchmark_label: str


class RangeCoverageOut(BaseModel):
    k: int
    n: int
    rate: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    nominal: float


class ReliabilityBinOut(BaseModel):
    p_mean: float
    hit_rate: float
    n: int


class TypeVerdictOut(BaseModel):
    type: TypeKey
    label: str
    n: int = Field(description="Rebalance dates: the picks of one date form one equal-weight basket.")
    n_needed: int | None = None
    n_issue_days: int = 0
    n_calls: int = 0
    n_delisted: int = 0
    call_hits: int = 0
    call_hit_rate: float | None = None
    hits: int
    hit_rate: float | None = None
    hit_ci: list[float] | None = None
    mean_excess: float | None = None
    mean_excess_ci: list[float] | None = None
    hac_lag: int = 0
    e_skill: float = Field(description="Current e-value (valid at any stopping time); the state uses it via e-BH.")
    e_harm: float
    e_skill_peak: float = 1.0
    e_harm_peak: float = 1.0
    n_tests: int = 0
    e_skill_crossed_strong: bool = False
    state: EvidenceState
    benchmarked: bool = True
    benchmark_label: str = ""
    brier: float | None = None
    bss: float | None = None
    bss_ci: list[float] | None = None
    n_stated_p: int = 0
    spiegelhalter_z: float | None = None
    reliability: list[ReliabilityBinOut] = Field(default_factory=list)
    range_coverage: RangeCoverageOut | None = None
    calibration_caption: str | None = None
    next_resolution_at: str | None = None
    note: str | None = None


class VerdictOut(BaseModel):
    headline: str
    resolved_calls: int
    resolved_units: int = 0
    n_needed: int | None = None
    target_hit_rate: float | None = None
    n_tests: int = 0
    fdr_level: float | None = None
    skill: SkillOut | None = None
    state: EvidenceState
    types: list[TypeVerdictOut]
    frozen_at_issue: bool = True
    method_note: str


class CallOut(BaseModel):
    type: TypeKey
    id: str
    issued_at: str
    resolved_at: str
    subject: str
    call: str
    stated_p: float | None = None
    stated_range: list[float] | None = None
    outcome: float | None = None
    benchmark_outcome: float | None = None
    excess: float | None = None
    hit: bool
    verdict: str | None = None
    delisted: bool = False


class CallsOut(BaseModel):
    type: TypeKey | None = None
    total: int
    limit: int
    offset: int
    items: list[CallOut] = Field(default_factory=list)
    worst_misses: list[CallOut] = Field(default_factory=list)
    frozen_at_issue: bool = True


@router.get("/verdict", response_model=VerdictOut)
def get_verdict(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Headline sentence, resolved-call count, skill vs the passive core and the
    evidence state per prediction type (too early / skill / no evidence / harm)."""
    return build_verdict(db, user.id)


@router.get("/calls", response_model=CallsOut)
def get_calls(
    type: TypeKey | None = Query(None, description="Only this prediction type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Resolved calls, newest first, with the five worst misses against the ETF."""
    return list_calls(db, user.id, type=type, limit=limit, offset=offset)
