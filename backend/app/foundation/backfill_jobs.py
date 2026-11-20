"""Asynchronous price-backfill jobs (proposal P4).

Multi-year provider backfills are too slow to run inline on the request path.
This module persists a ``BackfillJob`` row, offloads the work to the shared job
system (``services/jobs.submit_job`` → APScheduler background thread with its
own DB session), and exposes a status lookup the client can poll.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import BackfillJob
from app.foundation.price_backfill import backfill_user_prices

logger = logging.getLogger(__name__)


def _create_job(db: Session, user_id: str, days: int) -> str:
    """Persist a queued BackfillJob and return its id."""
    job = BackfillJob(user_id=user_id, days=days, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job.id


def enqueue_backfill(db: Session, user_id: str, days: int = 1825) -> str:
    """Create a queued backfill job and offload it to the job system.

    Returns the job id for the caller to poll via ``get_backfill_status``.
    """
    # One-directional edge into the generic job infra (Phase E): lazy per
    # house style — domains do not import jobs at module level.
    from app.foundation.jobs import submit_job

    job_id = _create_job(db, user_id, days)
    submit_job(_run_backfill_job, kwargs={"job_id": job_id})
    return job_id


def _run_backfill_job(job_id: str) -> None:
    """Entry point invoked on the background thread; owns its own session."""
    from app.foundation.core.db import SessionLocal

    db = SessionLocal()
    try:
        _execute_backfill(db, job_id)
    finally:
        db.close()


def _execute_backfill(db: Session, job_id: str) -> None:
    """Run the backfill for *job_id*, recording progress and outcome on the row."""
    job = db.get(BackfillJob, job_id)
    if job is None:
        logger.error("backfill job %s not found", job_id)
        return

    job.status = "running"
    job.started_at = datetime.now(UTC)
    db.commit()

    try:
        result = backfill_user_prices(db, job.user_id, days=job.days)
        job.total = int(result.get("total", 0))
        job.succeeded = int(result.get("succeeded", 0))
        job.failed = int(result.get("failed", 0))
        job.result_json = json.dumps(result.get("results", []))[:20000]
        job.status = "succeeded"
    except Exception as exc:  # noqa: BLE001 — record any failure on the row
        db.rollback()
        job = db.get(BackfillJob, job_id)
        if job is not None:
            job.status = "failed"
            job.error_message = str(exc)[:1000]
        logger.error("backfill job %s failed: %s", job_id, exc, exc_info=True)
    finally:
        if job is not None:
            job.finished_at = datetime.now(UTC)
            db.commit()


def get_backfill_status(
    db: Session, job_id: str, user_id: str | None = None
) -> dict[str, Any] | None:
    """Return the status payload for *job_id*, scoped to *user_id* when given."""
    query = db.query(BackfillJob).filter(BackfillJob.id == job_id)
    if user_id is not None:
        query = query.filter(BackfillJob.user_id == user_id)
    job = query.first()
    if job is None:
        return None
    return {
        "job_id": job.id,
        "status": job.status,
        "days": job.days,
        "total": job.total,
        "succeeded": job.succeeded,
        "failed": job.failed,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
