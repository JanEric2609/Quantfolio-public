"""Graduation state machine (PR2 D2).

``apply_graduation_state`` turns an ``evaluate_graduation`` verdict into the
persisted per-user switch (``AdvisorGraduationState``) and records every flip
as an ``AdvisorGraduationTransition`` with a reason — graduation AND
de-graduation are audited.

``is_graduated`` is reported on the advisor's what-would-change diff. Since
report Phase 4 it no longer makes that diff actionable: the LLM loop stays
paper-only, and only the monthly plan sets weights for the real book, from
strategies with a passing evidence card.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.foundation.models.entities import AdvisorGraduationState, AdvisorGraduationTransition
from app.foundation.models.entities._core import now_utc
from app.decision.graduation.evaluator import GraduationResult

logger = logging.getLogger(__name__)


def get_state(db: Session, user_id: str) -> AdvisorGraduationState | None:
    return (
        db.query(AdvisorGraduationState)
        .filter(AdvisorGraduationState.user_id == user_id)
        .one_or_none()
    )


def is_graduated(db: Session, user_id: str) -> bool:
    """The one switch: may the champion emit real rec cards right now?"""
    state = get_state(db, user_id)
    return bool(state is not None and state.graduated)


def _transition_reason(result: GraduationResult, to_graduated: bool) -> str:
    blocking = [c for c in result.criteria if c.blocking]
    if to_graduated:
        return f"all {len(blocking)} blocking criteria passed"
    failing = [c.key for c in blocking if not c.passed]
    return "criteria failing: " + ", ".join(failing) if failing else "evaluation not graduated"


def apply_graduation_state(
    db: Session,
    user_id: str,
    result: GraduationResult,
) -> AdvisorGraduationState:
    """Persist the verdict; record a transition when the switch flips.

    Idempotent for an unchanged verdict — only actual graduate/de-graduate
    flips create transition rows.
    """
    from app.decision.advisor.strategy import get_champion

    champion = get_champion(db, user_id)
    strategy_id = champion.id if champion is not None else None

    state = get_state(db, user_id)
    if state is None:
        state = AdvisorGraduationState(user_id=user_id, graduated=False)
        db.add(state)
        db.flush()

    now = now_utc()
    if bool(state.graduated) != bool(result.graduated):
        reason = _transition_reason(result, result.graduated)
        db.add(
            AdvisorGraduationTransition(
                user_id=user_id,
                strategy_id=strategy_id,
                from_graduated=bool(state.graduated),
                to_graduated=bool(result.graduated),
                reason=reason,
                criteria_json={
                    c.key: {"passed": c.passed, "value": c.value, "target": c.target}
                    for c in result.criteria
                },
            )
        )
        state.graduated = bool(result.graduated)
        state.since = now if result.graduated else None
        state.last_transition_at = now
        state.last_reason = reason
        logger.info(
            "graduation state for %s flipped to %s: %s", user_id, result.graduated, reason
        )

    state.strategy_id = strategy_id
    state.details_json = {
        "overall_progress": result.overall_progress,
        "generated_at": result.generated_at,
    }
    db.commit()
    db.refresh(state)
    return state
