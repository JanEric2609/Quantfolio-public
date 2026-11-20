"""Research API endpoints — LLM research, Obsidian sync."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user

router = APIRouter(tags=["quant-research"])


@router.post("/research/sync")
def sync_research(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.research_sync import sync_to_obsidian
    return sync_to_obsidian(db, user.id)


@router.get("/research/summary")
def research_summary(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    from app.foundation.research_sync import get_research_summary
    return get_research_summary(db, user.id)


@router.get("/research/llm-propose")
async def research_llm_propose(
    n: int = Query(default=3, ge=1, le=10),
    force_local: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.llm_research import propose_factors

    proposals, reasoning = await propose_factors(
        db, user.id, n_proposals=n, force_local=force_local,
    )
    return {
        "proposals": [
            {
                "name": p.name,
                "description": p.description,
                "formula": p.formula,
                "intuition": p.intuition,
                "expected_regime": p.expected_regime,
                "confidence": p.confidence,
            }
            for p in proposals
        ],
        "reasoning": reasoning,
        "count": len(proposals),
    }


@router.get("/research/llm-analyze")
async def research_llm_analyze(
    focus: str = Query(default="general", pattern="^(general|regime|factors|portfolio)$"),
    force_local: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.llm_research import analyze_market

    analysis, reasoning = await analyze_market(
        db, user.id, focus=focus, force_local=force_local,
    )
    return {
        "summary": analysis.summary,
        "regime_assessment": analysis.regime_assessment,
        "risks": analysis.risks,
        "opportunities": analysis.opportunities,
        "factor_implications": analysis.factor_implications,
        "confidence": analysis.confidence,
        "reasoning": reasoning,
    }


@router.get("/research/llm-regime-eval")
async def research_llm_regime_eval(
    from_regime: str = Query(...),
    to_regime: str = Query(...),
    force_local: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.llm_research import evaluate_regime_transition
    from app.lab.regime.gate import REGIME_LABEL_INDEX

    valid_regimes = set(REGIME_LABEL_INDEX.keys())
    if from_regime not in valid_regimes or to_regime not in valid_regimes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid regime. Must be one of: {sorted(valid_regimes)}",
        )

    evaluation, reasoning = await evaluate_regime_transition(
        db, user.id, from_regime, to_regime, force_local=force_local,
    )
    return {
        "from_regime": evaluation.from_regime,
        "to_regime": evaluation.to_regime,
        "plausibility": evaluation.plausibility,
        "implications": evaluation.implications,
        "recommended_actions": evaluation.recommended_actions,
        "factor_adjustments": evaluation.factor_adjustments,
        "reasoning": reasoning,
    }


@router.get("/research/obsidian-sync-status")
def research_obsidian_sync_status(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.obsidian_sync import get_sync_status
    return get_sync_status(db)


@router.post("/research/obsidian-sync")
def research_obsidian_sync(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.obsidian_sync import sync_hypotheses_to_factors
    return sync_hypotheses_to_factors(db, user.id)
