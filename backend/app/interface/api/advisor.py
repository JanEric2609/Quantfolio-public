"""Advisor loop API (PR1 G1): read-only tracer view of the latest cycle.

Surfaces, for the latest advisor cycle: the trades the LLM made, each trade's
thesis + confidence, the MC P5/P50/P95, risk-gate blocks, and the current
4-axis scorecard ("pending" until horizons mature). Honest numbers only — a
missing value is null, never a placeholder.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.auth import current_user
from app.foundation.models.entities import (
    AdvisorScorecard,
    LlmPortfolioDecision,
    PaperPortfolio,
    PaperTrade,
    User,
)
from app.decision.advisor.cycle import ADVISOR_MANDATE, run_advisor_cycle

router = APIRouter(prefix="/api/advisor", tags=["advisor"])


def _advisor_portfolio(db: Session, user_id: str) -> PaperPortfolio | None:
    return (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == ADVISOR_MANDATE)
        .one_or_none()
    )


def _scorecard_payload(row: AdvisorScorecard | None) -> dict[str, Any] | None:
    if row is None:
        return None
    details: dict[str, Any] = row.details_json or {}
    return {
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "n_predictions": row.n_predictions,
        "n_resolved": row.n_resolved,
        "risk_adjusted_return": {
            "sharpe": row.sharpe,
            "sortino": row.sortino,
            "calmar": row.calmar,
            # Lo (2002) standard error of the annualised Sharpe, plus the
            # "this interval spans zero, do not rank on it" flag. A bare
            # Sharpe point estimate on the sample sizes this app has is
            # misleading — see quant_metrics.sharpe_standard_error.
            "sharpe_se": details.get("sharpe_se"),
            "sharpe_ci_spans_zero": details.get("sharpe_ci_spans_zero"),
            "n_nav_returns": details.get("n_nav_returns"),
        },
        "calibration": {
            "brier_avg": row.brier_avg,
            "log_loss_avg": row.log_loss_avg,
        },
        "magnitude_accuracy": {
            "mz_slope": row.mz_slope,
            "mz_r2": row.mz_r2,
        },
        "downside_discipline": {
            "max_drawdown": row.max_drawdown,
            "cvar_95": row.cvar_95,
        },
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }


@router.get("/cycle/latest")
def latest_cycle(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Latest advisor-cycle decision + trades + scorecard for the tracer view."""
    portfolio = _advisor_portfolio(db, user.id)
    if portfolio is None:
        return {"portfolio_id": None, "decision": None, "trades": [], "scorecard": None}

    decision_row = (
        db.query(LlmPortfolioDecision)
        .filter(
            LlmPortfolioDecision.portfolio_id == portfolio.id,
            LlmPortfolioDecision.mandate == ADVISOR_MANDATE,
        )
        .order_by(LlmPortfolioDecision.review_date.desc())
        .first()
    )
    decision: dict[str, Any] | None = None
    if decision_row is not None:
        try:
            payload = json.loads(decision_row.decision_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        decision = {
            "review_date": decision_row.review_date.isoformat() if decision_row.review_date else None,
            "status": decision_row.status,
            "error": decision_row.error,
            **payload,
        }

    trades = (
        db.query(PaperTrade)
        .filter(PaperTrade.portfolio_id == portfolio.id)
        .order_by(PaperTrade.date.desc())
        .limit(20)
        .all()
    )
    scorecard_row = (
        db.query(AdvisorScorecard)
        .filter(AdvisorScorecard.portfolio_id == portfolio.id)
        .order_by(AdvisorScorecard.window_end.desc())
        .first()
    )

    return {
        "portfolio_id": portfolio.id,
        "decision": decision,
        "trades": [
            {
                "id": t.id,
                "ticker": t.ticker,
                "side": t.side,
                "quantity": float(t.quantity),
                "price": float(t.price),
                "value": float(t.value),
                "fee": float(t.fee or 0),
                "confidence": t.confidence,
                "rationale": t.rationale,
                "executed_at": t.date.isoformat() if t.date else None,
            }
            for t in trades
        ],
        "scorecard": _scorecard_payload(scorecard_row),
    }


@router.post("/cycle/run")
def trigger_cycle(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Manually trigger one advisor cycle (idempotent per day)."""
    result = run_advisor_cycle(db, user.id)
    # Prediction ids are internal; keep the payload lean.
    result["predictions"] = len(result.get("predictions") or [])
    return result


def _strategy_payload(db: Session, strategy: Any) -> dict[str, Any] | None:
    if strategy is None:
        return None
    from app.decision.advisor.evolution import composite_score, next_resolution_at

    card = (
        db.query(AdvisorScorecard)
        .filter(AdvisorScorecard.portfolio_id == strategy.portfolio_id)
        .order_by(AdvisorScorecard.window_end.desc())
        .first()
        if strategy.portfolio_id
        else None
    )
    score, detail = composite_score(card)
    return {
        "id": strategy.id,
        "role": strategy.role,
        "portfolio_id": strategy.portfolio_id,
        "config": strategy.config_json or {},
        "parent_strategy_id": strategy.parent_strategy_id,
        "promoted_at": strategy.promoted_at.isoformat() if strategy.promoted_at else None,
        "created_at": strategy.created_at.isoformat() if strategy.created_at else None,
        "scorecard": _scorecard_payload(card),
        "composite": score,
        "composite_detail": detail,
        # ISO datetime of the earliest still-pending prediction's resolve_at
        # for this sleeve, or None if there's nothing left to resolve. Lets
        # the UI show "next evaluation: ~{date}" instead of a bare
        # "insufficient data" while the composite is withheld.
        "next_resolution_at": (
            next_resolution_at(db, strategy.portfolio_id) if strategy.portfolio_id else None
        ),
    }


@router.get("/evolution")
def evolution_status(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Champion vs challenger standings, promotion history, lessons feed (F1)."""
    from app.foundation.models.entities import (
        AdvisorStrategy,
        CompetitionDecision,
        CompetitionRun,
        StrategyLesson,
    )
    from app.decision.advisor.evolution import EVOLUTION_RUN_NAME
    from app.decision.advisor.strategy import get_active_challenger, get_champion

    champion = get_champion(db, user.id)
    challenger = get_active_challenger(db, user.id)

    sleeve_ids = [
        s.portfolio_id
        for s in db.query(AdvisorStrategy).filter(AdvisorStrategy.user_id == user.id).all()
        if s.portfolio_id
    ]
    rounds: list[dict[str, Any]] = []
    if sleeve_ids:
        run_rows = (
            db.query(CompetitionRun)
            .filter(
                CompetitionRun.name == EVOLUTION_RUN_NAME,
                CompetitionRun.portfolio_a_id.in_(sleeve_ids),
            )
            .order_by(CompetitionRun.created_at.desc())
            .limit(10)
            .all()
        )
        for run in run_rows:
            decisions = (
                db.query(CompetitionDecision)
                .filter(CompetitionDecision.run_id == run.id)
                .order_by(CompetitionDecision.round_number.desc())
                .limit(20)
                .all()
            )
            rounds.append(
                {
                    "run_id": run.id,
                    "status": run.status,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "ended_at": run.ended_at.isoformat() if run.ended_at else None,
                    "decisions": [
                        {
                            "round": d.round_number,
                            "portfolio_id": d.portfolio_id,
                            "winner": d.winner,
                            "score": json.loads(d.score_json or "{}"),
                            "created_at": d.created_at.isoformat() if d.created_at else None,
                        }
                        for d in decisions
                    ],
                }
            )

    lessons = (
        db.query(StrategyLesson)
        .filter(StrategyLesson.user_id == user.id)
        .order_by(StrategyLesson.created_at.desc())
        .limit(30)
        .all()
    )
    promotions = (
        db.query(AdvisorStrategy)
        .filter(AdvisorStrategy.user_id == user.id, AdvisorStrategy.promoted_at.isnot(None))
        .order_by(AdvisorStrategy.promoted_at.desc())
        .limit(20)
        .all()
    )

    return {
        "champion": _strategy_payload(db, champion),
        "challenger": _strategy_payload(db, challenger),
        "rounds": rounds,
        "promotion_history": [
            {
                "strategy_id": s.id,
                "role": s.role,
                "promoted_at": s.promoted_at.isoformat() if s.promoted_at else None,
                "retired_at": s.retired_at.isoformat() if s.retired_at else None,
                "parent_strategy_id": s.parent_strategy_id,
            }
            for s in promotions
        ],
        "lessons": [
            {
                "id": lesson.id,
                "strategy_id": lesson.strategy_id,
                "text": lesson.lesson_text,
                "tags": lesson.tags_json or {},
                "rank": lesson.rank,
                "active": lesson.active,
                "created_at": lesson.created_at.isoformat() if lesson.created_at else None,
            }
            for lesson in lessons
        ],
    }


@router.post("/evolution/run")
def trigger_evolution_round(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Manually trigger one champion-vs-challenger evolution round."""
    from app.decision.advisor.evolution import run_evolution_round

    return run_evolution_round(db, user.id)


@router.get("/diagnostics")
def advisor_diagnostics(
    probe_llm: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """End-to-end health check of the advisor loop.

    Walks every stage — scheduler, sleeves, Discover candidate feed, price
    history, LLM endpoint, cycle history, cash headroom, trade activity,
    prediction ledger, scorecard, learning loop — and reports where the chain
    is severed. Stages are returned in pipeline order so the first failure is
    the one to fix.

    Args:
        probe_llm: When true, makes a real call to the configured LLM endpoint
            using the advisor's own structured-JSON contract. Off by default
            because it costs a round trip.
    """
    from app.decision.advisor.diagnostics import run_advisor_diagnostics

    return run_advisor_diagnostics(db, user.id, probe_llm=probe_llm)


@router.get("/what-would-change")
def what_would_change(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Position-level champion-vs-real diff (E1). Preview when ungraduated."""
    from app.decision.advisor.recommendations import compute_what_would_change

    return compute_what_would_change(db, user.id)
