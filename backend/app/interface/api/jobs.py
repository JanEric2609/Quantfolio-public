"""API endpoints for scheduled job run tracking."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import JobRun, User
from app.foundation.auth import current_user

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobRunResponse(BaseModel):
    id: str
    job_name: str
    status: str
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    error_message: str | None
    triggered_by: str


class JobRunsListResponse(BaseModel):
    runs: list[JobRunResponse]
    total: int


class JobSummary(BaseModel):
    job_name: str
    last_run: str | None
    last_status: str | None
    avg_duration_ms: int | None
    total_runs: int
    success_rate: float


@router.get("", response_model=JobRunsListResponse)
def list_job_runs(
    job_name: str | None = Query(None, description="Filter by job name"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List job runs with optional filtering."""
    query = db.query(JobRun)
    if job_name:
        query = query.filter(JobRun.job_name == job_name)
    if status:
        query = query.filter(JobRun.status == status)
    total = query.count()
    runs = (
        query.order_by(desc(JobRun.started_at))
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "runs": [
            JobRunResponse(
                id=r.id,
                job_name=r.job_name,
                status=r.status,
                started_at=r.started_at.isoformat() if r.started_at else "",
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
                duration_ms=r.duration_ms,
                error_message=r.error_message,
                triggered_by=r.triggered_by,
            )
            for r in runs
        ],
        "total": total,
    }


@router.get("/summary", response_model=list[JobSummary])
def job_summary(
    _user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[JobSummary]:
    """Get summary statistics for each job name."""
    from sqlalchemy import func, case

    # Subquery to get the most recent run per job name
    subq = (
        db.query(
            JobRun.job_name,
            JobRun.status.label("latest_status"),
            JobRun.started_at.label("latest_started_at"),
            func.row_number().over(
                partition_by=JobRun.job_name,
                order_by=JobRun.started_at.desc()
            ).label("rn"),
        )
        .subquery()
    )

    latest_runs = (
        db.query(subq.c.job_name, subq.c.latest_status)
        .filter(subq.c.rn == 1)
        .all()
    )
    latest_status_map = {r.job_name: r.latest_status for r in latest_runs}

    results = (
        db.query(
            JobRun.job_name,
            func.max(JobRun.started_at).label("last_run"),
            func.avg(JobRun.duration_ms).label("avg_duration_ms"),
            func.count(JobRun.id).label("total_runs"),
            func.sum(case((JobRun.status == "success", 1), else_=0)).label("successful_runs"),
        )
        .group_by(JobRun.job_name)
        .order_by(desc("last_run"))
        .all()
    )

    summaries: list[JobSummary] = []
    for row in results:
        total = row.total_runs or 1
        successful = row.successful_runs or 0
        success_rate = (successful / total) * 100 if total > 0 else 0

        summaries.append(
            JobSummary(
                job_name=row.job_name,
                last_run=row.last_run.isoformat() if row.last_run else None,
                last_status=latest_status_map.get(row.job_name),
                avg_duration_ms=int(row.avg_duration_ms) if row.avg_duration_ms else None,
                total_runs=row.total_runs,
                success_rate=round(success_rate, 1),
            )
        )

    return summaries


class WorkerStatus(BaseModel):
    started_at: str
    beat_at: str
    alive: bool


class ScheduledJob(BaseModel):
    id: str
    schedule: str | None = None
    next_run: str | None = None
    last_run: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    avg_duration_ms: int | None = None
    total_runs: int = 0
    success_rate: float | None = None


class JobScheduleResponse(BaseModel):
    # None until a worker with the heartbeat has run: every job then lands in
    # ``jobs`` without a schedule, since live and retired can't be told apart.
    worker: WorkerStatus | None
    jobs: list[ScheduledJob]
    retired: list[ScheduledJob]


@router.get("/schedule", response_model=JobScheduleResponse)
def job_schedule(
    _user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> JobScheduleResponse:
    """Registered jobs (from the worker heartbeat) joined with their run history.

    Job names that only exist in the run history are retired: nothing in the
    worker schedules them any more.
    """
    from datetime import UTC, datetime, timedelta

    from app.foundation.jobs import WORKER_HEARTBEAT_KEY
    from app.foundation.settings import get_setting_row

    history = {s.job_name: s for s in job_summary(_user=_user, db=db)}
    last_errors = _last_errors(db)

    def merged(job_id: str, schedule: str | None = None, next_run: str | None = None) -> ScheduledJob:
        summary = history.get(job_id)
        return ScheduledJob(
            id=job_id,
            schedule=schedule,
            next_run=next_run,
            last_run=summary.last_run if summary else None,
            last_status=summary.last_status if summary else None,
            last_error=last_errors.get(job_id) if summary and summary.last_status == "error" else None,
            avg_duration_ms=summary.avg_duration_ms if summary else None,
            total_runs=summary.total_runs if summary else 0,
            success_rate=summary.success_rate if summary else None,
        )

    heartbeat = get_setting_row(db, WORKER_HEARTBEAT_KEY)
    if not isinstance(heartbeat, dict):
        return JobScheduleResponse(worker=None, jobs=[merged(name) for name in history], retired=[])

    beat_at = datetime.fromisoformat(heartbeat["beat_at"])
    grace = timedelta(minutes=3 * int(heartbeat.get("interval_minutes", 5)))
    worker = WorkerStatus(
        started_at=heartbeat["started_at"],
        beat_at=heartbeat["beat_at"],
        alive=datetime.now(UTC) - beat_at <= grace,
    )
    registered = {job["id"]: job for job in heartbeat.get("jobs", []) if job["id"] != "worker_heartbeat"}
    jobs = [merged(job_id, job.get("schedule"), job.get("next_run")) for job_id, job in registered.items()]
    retired = [merged(name) for name in history if name not in registered]
    return JobScheduleResponse(worker=worker, jobs=jobs, retired=retired)


def _last_errors(db: Session) -> dict[str, str]:
    """The newest error message per job name."""
    from sqlalchemy import func

    newest = (
        db.query(JobRun.job_name, func.max(JobRun.started_at).label("started_at"))
        .filter(JobRun.status == "error")
        .group_by(JobRun.job_name)
        .subquery()
    )
    rows = (
        db.query(JobRun.job_name, JobRun.error_message)
        .join(newest, (JobRun.job_name == newest.c.job_name) & (JobRun.started_at == newest.c.started_at))
        .all()
    )
    return {name: message for name, message in rows if message}
