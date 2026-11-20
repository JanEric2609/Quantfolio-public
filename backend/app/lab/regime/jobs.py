"""Scheduler registrations for the regime models (Phase E relocation).

Registrars moved byte-identical out of ``services/jobs.py`` so the generic
job infrastructure no longer imports owning contexts. The composition root
(``app.worker.build_scheduler``) calls these by name.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


def register_regime_daily_job(scheduler: Any | None = None) -> str:
    """Register the daily regime classify-and-store job.

    Runs at 07:15 UTC every day (after macro_refresh at 07:00, before the
    change detector at 07:30). Reads settings, loads bars, and writes a
    regime_snapshot with source='hmm'.
    """

    def regime_daily_inner() -> dict[str, Any] | None:
        from app.foundation.core.db import SessionLocal
        from app.lab.regime.classifier import classify_and_store

        db = SessionLocal()
        try:
            result = classify_and_store(db)
            logger.info("regime_daily: written=%s label=%s", result.get("written"), result.get("label"))
            if not result.get("written"):
                return {"status": "warning", "reason": result.get("reason", "not_written")}
            return None
        except Exception as exc:
            logger.error("regime_daily failed: %s", exc, exc_info=True)
            raise
        finally:
            db.close()

    return register_cron_job(
        "regime_daily", regime_daily_inner,
        scheduler=scheduler, hour=7, minute=15,
    )

def register_regime_refit_job(scheduler: Any | None = None) -> str:
    """Register the weekly regime HMM refit job.

    Runs every Sunday at 06:00 UTC. Fits a fresh RegimeHMM on recent history
    and persists it so that the daily classify job always uses an up-to-date model.
    """

    def regime_refit_inner() -> dict[str, Any]:
        from app.foundation.core.db import SessionLocal
        from app.lab.regime.classifier import refit_regime_model

        db = SessionLocal()
        try:
            result = refit_regime_model(db)
        finally:
            db.close()
        logger.info(
            "regime_refit: hmm saved=%s model_id=%s jump=%s n_rows=%s",
            result.get("saved"),
            result.get("model_id"),
            result.get("jump"),
            result.get("n_rows"),
        )
        # Audit 2026-11-15, 2.4: a fit that did not converge kept the old model while the run said success.
        jump = result.get("jump") or {}
        problems = []
        if not jump.get("saved"):
            problems.append(f"jump model not saved: {jump.get('reason') or result.get('reason') or 'unknown'}")
        if not result.get("saved"):
            problems.append(f"HMM not saved: {result.get('reason') or 'unknown'}")
        if problems:
            both = len(problems) == 2
            return {"status": "failed" if both else "degraded", "reason": "; ".join(problems), "n_rows": result.get("n_rows") or 0}
        return {"status": "ok"}

    return register_cron_job(
        "regime_refit", regime_refit_inner,
        scheduler=scheduler, day_of_week="sun", hour=6, minute=0,
    )
