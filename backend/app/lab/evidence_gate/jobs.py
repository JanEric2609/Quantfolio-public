"""Scheduler registration for the evidence gate.

The gate only regrades the monthly records ``pooled_model`` and ``satellite``
already wrote next to the panel (a few JSON files and a ledger count), so it is
cheap and safe in the worker: it builds no panel and trains no model. It keeps
the verdict, and the trial count the monthly plan quotes, from going stale.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.factor_evidence import TILT_EVIDENCE_REGION
from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


def register_evidence_gate_job(scheduler: Any | None = None) -> str:
    """Register the weekly evidence-gate regrade (Sunday 05:30 UTC, before the 06:00 regime refit)."""

    def evidence_gate_inner() -> dict[str, Any] | None:
        from app.foundation.core.db import SessionLocal
        from app.lab.evidence_gate.gate import run_evidence_gate

        db = SessionLocal()
        try:
            result = run_evidence_gate(db, region=TILT_EVIDENCE_REGION, persist=True)
        except Exception as exc:
            logger.error("evidence_gate failed: %s", exc, exc_info=True)
            raise
        finally:
            db.close()
        if result is None:
            logger.warning("evidence_gate: no stored results for %s; skipped", TILT_EVIDENCE_REGION)
            return {"status": "warning", "reason": "no_stored_results"}
        logger.info(
            "evidence_gate: n_trials=%s satellite_unlocked=%s", result.n_trials, result.satellite_unlocked,
        )
        return None

    return register_cron_job(
        "evidence_gate", evidence_gate_inner,
        scheduler=scheduler, day_of_week="sun", hour=5, minute=30,
    )
