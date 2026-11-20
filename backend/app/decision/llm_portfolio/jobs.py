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


def _journal_failure(db: Any, user_id: str, mandate: str, exc: BaseException) -> None:
    """Write a ``review_failed`` decision row so the journal shows the failed week."""
    import json
    from datetime import UTC, datetime

    from app.decision.llm_portfolio.mirror import ensure_mandate_portfolio
    from app.foundation.models.entities import LlmPortfolioDecision

    try:
        portfolio = ensure_mandate_portfolio(db, user_id, mandate)
        db.add(
            LlmPortfolioDecision(
                portfolio_id=portfolio.id,
                review_date=datetime.now(UTC),
                mandate=mandate,
                decision_json="{}",
                reflection_json=json.dumps({"crashed": True}),
                context_token_estimate=0,
                status="review_failed",
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 - journalling the failure is best effort
        db.rollback()
        logger.error("Could not journal the failed mandate %s review for %s", mandate, user_id, exc_info=True)


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

    def _review_all(mandate: str) -> dict[str, Any]:
        """Review every user's mandate; a failure is journalled and reported, not swallowed.

        Audit 2026-11-15, 2.1/2.4: a crash used to leave no decision row while
        ``job_runs`` said ``success``. Now the user's failure is written as a
        ``review_failed`` decision and the run ends ``degraded`` or ``failed``.
        """
        db = SessionLocal()
        ok = failed = 0
        errors: list[str] = []
        try:
            for user in db.query(User).all():
                try:
                    run_mandate_review(db, user.id, mandate)
                    ok += 1
                except Exception as e:  # noqa: BLE001 - one user's failure must not stop the others
                    db.rollback()
                    failed += 1
                    errors.append(f"{type(e).__name__}: {e}"[:300])
                    logger.error("LLM mandate %s review failed for %s: %s", mandate, user.id, e, exc_info=True)
                    _journal_failure(db, user.id, mandate, e)
        finally:
            db.close()
        if failed == 0:
            return {"status": "ok", "reviewed": ok}
        return {
            "status": "failed" if ok == 0 else "degraded",
            "reason": "; ".join(errors)[:600],
            "reviewed": ok,
            "failed": failed,
        }

    def _review_mandate_a() -> dict[str, Any]:
        return _review_all("A")

    def _review_mandate_b() -> dict[str, Any]:
        return _review_all("B")

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
