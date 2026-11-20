"""Scheduler registration for the nightly forward-test lab."""
from __future__ import annotations

from typing import Any

from app.foundation.jobs import register_cron_job


def register_forward_lab_job(scheduler: Any | None = None) -> str:
    """Score and resolve the forward-test lab at 04:30 UTC.

    After the universe prefetch (01:15 UTC, at most three hours) has stored
    the previous day's bars, and before any exchange opens.
    """
    from app.foundation.core.db import SessionLocal
    from app.lab.forward_lab import run_nightly

    def forward_lab_inner() -> None:
        with SessionLocal() as db:
            run_nightly(db)

    return register_cron_job("forward_lab_nightly", forward_lab_inner, scheduler=scheduler, hour=4, minute=30)


def register_news_tone_job(scheduler: Any | None = None) -> str:
    """Rate the last day's headlines at 05:00 UTC, after the lab (04:30) and the prefetch."""
    from app.foundation.core.db import SessionLocal
    from app.lab.forward_lab.news_tone import run_nightly

    def news_tone_inner() -> dict[str, Any]:
        with SessionLocal() as db:
            return run_nightly(db)

    return register_cron_job("news_tone_nightly", news_tone_inner, scheduler=scheduler, hour=5, minute=0)
