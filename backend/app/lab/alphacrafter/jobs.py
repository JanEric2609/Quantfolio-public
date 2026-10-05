"""Scheduler registrations for the AlphaCrafter pipeline (Phase E relocation).

Registrars moved byte-identical out of ``services/jobs.py`` so the generic
job infrastructure no longer imports owning contexts. The composition root
(``app.worker.build_scheduler``) calls these by name.
"""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_cron_job

logger = logging.getLogger(__name__)


def register_alphacrafter_daily_job(scheduler: Any | None = None) -> str:
    """Register the daily AlphaCrafter job (Phase 4).

    Runs the Miner → Screener → Trader → Dossier pipeline at 08:00 UTC, after
    the regime classify-and-store job (07:00) so the Screener gates on a fresh
    regime snapshot.
    """
    import asyncio

    from app.foundation.core.db import SessionLocal
    from app.lab.alphacrafter import run_daily_alphacrafter

    def alphacrafter_daily_inner() -> None:
        db = SessionLocal()
        try:
            asyncio.run(run_daily_alphacrafter(db))
        finally:
            db.close()

    return register_cron_job(
        "alphacrafter_daily", alphacrafter_daily_inner,
        scheduler=scheduler, hour=8, minute=0,
    )

def register_alphacrafter_tuning_job(scheduler: Any | None = None) -> str:
    """Register the QUARTERLY AlphaCrafter walk-forward tuning job.

    Runs the DSR-guarded grid search (services/alphacrafter/tuning.py) on
    Jan/Apr/Jul/Oct 1st at 06:15 UTC — before the daily pipeline (08:00) on
    those days, so re-selection is available to the same day's run. Re-selection
    happens at most quarterly; parameters stay frozen between runs.
    """
    from app.lab.alphacrafter.tuning import run_tuning_job_sync

    return register_cron_job(
        "alphacrafter_tuning", run_tuning_job_sync,
        scheduler=scheduler, month="1,4,7,10", day="1", hour=6, minute=15,
    )
