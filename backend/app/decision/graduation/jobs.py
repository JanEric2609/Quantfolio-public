"""Scheduler registration for the capital ladder (ADR 0025, plan 2026-11-10 WS4)."""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


def register_ladder_evidence_job(scheduler: Any | None = None) -> str:
    """Register the nightly capital-ladder evidence job.

    06:45 UTC daily: after the 00:00 paper snapshots (yesterday's NAV at its
    close) and the 06:30 price backfill (the benchmark's EUR close), and before
    the 07:10 monthly-plan snapshot that reads the ladder. Replaces the
    ``evaluate_graduation`` / ``apply_graduation_state`` call that used to run
    inside the evolution round.
    """

    def ladder_evidence_inner() -> None:
        from app.decision.graduation.ladder import run_ladder
        from app.foundation.core.db import SessionLocal
        from app.foundation.models.entities import User

        db = SessionLocal()
        try:
            for user in db.query(User).all():
                try:
                    state = run_ladder(db, user.id)
                    logger.info(
                        "ladder_evidence user=%s candidates=%d satellite=%.1f%%",
                        user.id, len(state["candidates"]), state["satellite_weight_pct"],
                    )
                except Exception:
                    logger.error("ladder_evidence failed for user %s", user.id, exc_info=True)
                    db.rollback()
        finally:
            db.close()

    return register_cron_job(
        "ladder_evidence", ladder_evidence_inner,
        scheduler=scheduler, hour=6, minute=45,
    )
