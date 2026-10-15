"""Scheduler registrations for the trust evidence ledgers (ADR 0018).

* ``trust_daily_ledger`` — nightly at 02:40 UTC, after the 02:00 prediction
  resolution: freezes the ``ideas`` and ``advisor`` daily active-return series.
  It refreshes prices for the open picks only (about 15-60 symbols), which
  nothing else keeps current.
* ``trust_weekly_ledger`` — Saturdays at 03:40 UTC, when Friday's closes are
  final: resolves the shadow ledger's candidate outcomes and freezes the
  ``ranking`` series. One price pass over the open cohorts' ~300 names a week,
  about the load of one Discover run, instead of every night.

Both are idempotent catch-ups: a missed night is filled in by the next run,
and nothing frozen is ever rewritten.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


def run_daily_ledger() -> dict[str, dict[str, int]]:
    from app.decision.verification.daily_ledger import (
        SERIES_ADVISOR,
        SERIES_IDEAS,
        freeze_user_series,
        ledger_users,
    )
    from app.foundation.core.db import SessionLocal

    db = SessionLocal()
    try:
        return {
            user_id: freeze_user_series(db, user_id, (SERIES_IDEAS, SERIES_ADVISOR), refresh=True)
            for user_id in ledger_users(db)
        }
    finally:
        db.close()


def run_weekly_ledger() -> dict[str, Any]:
    from app.decision.verification.candidate_outcomes import resolve_candidate_outcomes
    from app.decision.verification.daily_ledger import SERIES_RANKING, freeze_user_series, ledger_users
    from app.foundation.core.db import SessionLocal

    db = SessionLocal()
    try:
        try:
            outcomes = resolve_candidate_outcomes(db, refresh=True)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("trust_weekly_ledger: candidate outcomes failed")
            outcomes = -1
        ranking = {
            user_id: freeze_user_series(db, user_id, (SERIES_RANKING,), refresh=True)
            for user_id in ledger_users(db)
        }
        # The factor study (ADR 0018 §8) was withdrawn on 2026-10-14: the
        # selection family against matched non-picks does its job.
        return {"candidate_outcomes": outcomes, "ranking": ranking}
    finally:
        db.close()


def register_trust_daily_ledger_job(scheduler: Any | None = None) -> str:
    """Nightly freeze of the ideas and advisor daily active-return series."""

    def _inner() -> None:
        result = run_daily_ledger()
        logger.info("trust_daily_ledger: %s", result)

    return register_cron_job("trust_daily_ledger", _inner, scheduler=scheduler, hour=2, minute=40)


def register_trust_weekly_ledger_job(scheduler: Any | None = None) -> str:
    """Saturday shadow-ledger outcomes and ranking-series freeze."""

    def _inner() -> None:
        result = run_weekly_ledger()
        logger.info("trust_weekly_ledger: %s", result)

    return register_cron_job(
        "trust_weekly_ledger", _inner, scheduler=scheduler, day_of_week="sat", hour=3, minute=40,
    )
