"""Scheduler registrations for the LLM mandate review (Phase E relocation).

Registrars moved byte-identical out of ``services/jobs.py`` so the generic
job infrastructure no longer imports owning contexts. The composition root
(``app.worker.build_scheduler``) calls these by name.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


def register_llm_portfolio_review_jobs(scheduler: Any | None = None) -> str:
    """Register weekly LLM mandate review jobs for all users.

    Runs mandate A review every Tuesday at 18:30 UTC and mandate B review
    every Friday at 18:30 UTC.
    """
    from app.foundation.core.db import SessionLocal
    from app.foundation.models.entities import User

    try:
        from app.decision.llm_portfolio.review import run_mandate_review
    except ImportError:
        logger.warning("app.decision.llm_portfolio.review not available; LLM review jobs will be no-ops")

        def run_mandate_review(db: Any, user_id: str, mandate: str) -> dict[str, Any]:
            logger.info("Placeholder run_mandate_review for user=%s mandate=%s", user_id, mandate)
            return {"user_id": user_id, "mandate": mandate, "status": "placeholder"}

    def _review_mandate_a() -> None:
        db = SessionLocal()
        try:
            users = db.query(User).all()
            for user in users:
                try:
                    run_mandate_review(db, user.id, "A")
                except Exception as e:
                    db.rollback()
                    logger.error("LLM mandate A review failed for %s: %s", user.id, e)
        finally:
            db.close()

    def _review_mandate_b() -> None:
        db = SessionLocal()
        try:
            users = db.query(User).all()
            for user in users:
                try:
                    run_mandate_review(db, user.id, "B")
                except Exception as e:
                    db.rollback()
                    logger.error("LLM mandate B review failed for %s: %s", user.id, e)
        finally:
            db.close()

    jid_a = register_cron_job(
        "llm_mandate_a_review", _review_mandate_a,
        scheduler=scheduler, day_of_week="tue", hour=18, minute=30,
    )
    jid_b = register_cron_job(
        "llm_mandate_b_review", _review_mandate_b,
        scheduler=scheduler, day_of_week="fri", hour=18, minute=30,
    )
    return f"{jid_a},{jid_b}"

def register_llm_review_scoring_job(scheduler: Any | None = None) -> str:
    """Register the weekly mandate-review outcome-scoring job.

    Finds completed decisions whose ``horizon_weeks`` has elapsed and stamps a
    deterministic ``verdict`` (hit / miss / partial / unresolvable) by measuring
    the stated expectation against realised portfolio metrics. Runs Sunday 07:00
    UTC — after the weekly reviews (Tue/Fri) and daily snapshots have settled.
    """
    from app.foundation.core.db import SessionLocal

    try:
        from app.decision.llm_portfolio.scoring import run_scoring
    except ImportError:
        logger.warning("app.decision.llm_portfolio.scoring not available; scoring job will be a no-op")

        def run_scoring(db: Any = None) -> int:
            logger.info("Placeholder run_scoring — scoring module unavailable")
            return 0

    def _score_reviews() -> None:
        db = SessionLocal()
        try:
            count = run_scoring(db)
            logger.info("llm_review_scoring scored %d decisions", count)
        except Exception as exc:
            db.rollback()
            logger.error("llm_review_scoring failed: %s", exc, exc_info=True)
        finally:
            db.close()

    return register_cron_job(
        "llm_review_scoring", _score_reviews,
        scheduler=scheduler, day_of_week="sun", hour=7, minute=0,
    )
