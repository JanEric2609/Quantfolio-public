"""Scheduler registrations for the trust evidence ledgers (ADR 0018).

* ``trust_daily_ledger`` — nightly at 02:40 UTC, after the 02:00 prediction
  resolution: freezes the ``ideas`` and ``advisor`` daily active-return series.
  It refreshes prices for the open picks only (about 15-60 symbols), which
  nothing else keeps current.
* ``trust_weekly_ledger`` — Saturdays at 03:40 UTC, when Friday's closes are
  final: resolves the shadow ledger's candidate outcomes and freezes the
  ``ranking`` series. It then records the factor study (ADR 0018 §8) once
  there is enough back-filled data, and freezes the factor-neutral row only
  if the study said ``build``. One price pass over the open cohorts' ~300 names a week,
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


def _run_factor_study(db: Any) -> dict[str, Any]:
    """ADR 0018 §8: record the study once it can be, then freeze the gated row."""
    from app.decision.verification.daily_ledger import ledger_users
    from app.decision.verification.factor_study import freeze_neutral_series, run_factor_study

    out: dict[str, Any] = {}
    for user_id in ledger_users(db):
        try:
            study = run_factor_study(db, user_id, refresh_factors=True)
            written = freeze_neutral_series(db, user_id, refresh_factors=True)
            db.commit()
            out[user_id] = {"study": study.get("status"), "decision": study.get("decision"), "neutral_days": written}
        except Exception:
            db.rollback()
            logger.exception("trust_weekly_ledger: factor study failed for user %s", user_id)
            out[user_id] = {"study": "failed"}
    return out


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
        return {"candidate_outcomes": outcomes, "ranking": ranking, "factor_study": _run_factor_study(db)}
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
