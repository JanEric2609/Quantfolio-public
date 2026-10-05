"""Generic background-job service shared by ML, RL, and Qlib runners.

Wraps APScheduler's BackgroundScheduler. Jobs are fire-and-forget; status is
tracked by writing to the relevant DB row (QuantRun.type, etc.).
The scheduler is started lazily on first use.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_scheduler = None
_lock = threading.Lock()

# Scheduler defaults for every APScheduler instance in this codebase (the
# worker's BlockingScheduler and the API's on-demand BackgroundScheduler).
# APScheduler's own defaults (misfire_grace_time=1 s, 10 threads) dropped a run
# whenever the pool was busy for more than one second at its trigger time — the
# worker registers ~40 jobs, several at the same minute (06:30, 20:00, 02:00).
# A run that could not start within the grace window is skipped, not queued.
#   misfire_grace_time  a delayed run still fires if it can start within 5 min
#   coalesce            runs that piled up while the pool was busy collapse to one
#   max_instances       a job never overlaps itself (a slow run skips its next tick)
SCHEDULER_JOB_DEFAULTS: dict[str, Any] = {
    "misfire_grace_time": 300,
    "coalesce": True,
    "max_instances": 1,
}

# Worker thread pool. The jobs are I/O-bound (provider calls, LLM calls, SQL),
# and ~40 of them share a handful of trigger minutes. Each running job holds one
# DB connection (two for a tracked job on PostgreSQL: session + advisory lock),
# which is why foundation/core/db.py sizes the connection pool to match.
WORKER_THREAD_POOL_SIZE = 16


def scheduler_job_defaults() -> dict[str, Any]:
    """A fresh copy of :data:`SCHEDULER_JOB_DEFAULTS` (APScheduler keeps what it is given)."""
    return dict(SCHEDULER_JOB_DEFAULTS)


def scheduler_executors(max_workers: int) -> dict[str, Any]:
    """The default thread-pool executor, sized explicitly instead of APScheduler's 10."""
    from apscheduler.executors.pool import ThreadPoolExecutor

    return {"default": ThreadPoolExecutor(max_workers)}


def build_worker_scheduler() -> Any:
    """The worker's blocking scheduler, configured with the defaults above."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    return BlockingScheduler(
        timezone="UTC",
        job_defaults=scheduler_job_defaults(),
        executors=scheduler_executors(WORKER_THREAD_POOL_SIZE),
    )


def _release_advisory_lock(lock_conn: Any, job_name: str) -> None:
    """Release and close a session-level advisory lock connection.

    Every exit path out of :func:`_track_job` that acquired a lock must route
    through here. The lock is session-level, so a connection returned to the
    pool still holding it leaks the lock until the process exits and every
    later run of the same job name blocks forever.
    """
    if lock_conn is None:
        return
    try:
        lock_conn.exec_driver_sql(
            "SELECT pg_advisory_unlock(hashtext(%(key)s))", {"key": job_name}
        )
    except Exception as unlock_exc:
        logger.error(
            "Failed to release advisory lock for %s: %s", job_name, unlock_exc, exc_info=True
        )
        # Discard the raw connection so the server drops the session-level
        # lock instead of leaking it into the pool.
        try:
            lock_conn.invalidate()
        except Exception:
            pass
    finally:
        lock_conn.close()


def _track_job(job_name: str, job_fn: Callable, triggered_by: str = "scheduler") -> None:
    """Wrap a job function with JobRun tracking (start, success/fail, duration).

    Uses SELECT ... FOR UPDATE to serialise concurrent execution of the same
    named job across workers, preventing duplicate ``status='running'`` rows.
    """

    from app.foundation.core.db import SessionLocal
    from app.foundation.models.entities import JobRun

    started = datetime.now(UTC)
    db = SessionLocal()
    dialect = db.bind.dialect.name if db.bind else "sqlite"

    # Session-level advisory lock (PostgreSQL only) — held across commits,
    # prevents cross-worker races during the entire job_fn execution.
    # The lock MUST live on a dedicated connection held open for the whole
    # job: a Session releases its pooled connection at every commit, so
    # locking through `db` pins the lock to whichever pooled connection
    # served that statement — the later unlock then runs on a different
    # connection and silently fails ("you don't own a lock"), leaking the
    # lock on an idle pooled connection until process exit. Leaked locks
    # cross-held on shared pooled connections caused daily scheduler jams
    # and pg deadlocks between unrelated job names.
    lock_conn = None
    if dialect == "postgresql" and isinstance(db.bind, Engine):
        lock_conn = db.bind.connect()
        lock_conn.exec_driver_sql(
            "SELECT pg_advisory_lock(hashtext(%(key)s))", {"key": job_name}
        )

    try:
        run = JobRun(
            id=str(uuid.uuid4()),
            job_name=job_name,
            status="running",
            started_at=started,
            triggered_by=triggered_by,
        )
        db.add(run)
        db.commit()
    except Exception as exc:
        logger.error("Failed to create JobRun for %s: %s", job_name, exc)
        # The advisory lock is already held at this point. Returning without
        # releasing it strands a session-level lock on a connection that is
        # never reused, permanently blocking every later run of this job name.
        _release_advisory_lock(lock_conn, job_name)
        db.close()
        return

    try:
        outcome = job_fn()
        finished = datetime.now(UTC)
        # Opt-in sentinel protocol: a job_fn may return {"status": "warning",
        # "reason": ...} to report that it ran to completion but declined to
        # do its job for a documented, non-exceptional reason (e.g. not
        # enough data yet). Every job function today returns None, so this
        # is backward compatible everywhere except call sites that opt in.
        if isinstance(outcome, dict) and outcome.get("status") == "warning":
            run.status = "warning"
            run.error_message = str(outcome.get("reason") or "")[:1000]
        else:
            run.status = "success"
        run.finished_at = finished
        run.duration_ms = int((finished - started).total_seconds() * 1000)
    except Exception as exc:
        finished = datetime.now(UTC)
        run.status = "error"
        run.error_message = str(exc)[:1000]
        run.finished_at = finished
        run.duration_ms = int((finished - started).total_seconds() * 1000)
        logger.error("Job %s failed: %s", job_name, exc, exc_info=True)
        _alert_job_failure(db, job_name, str(exc)[:1000])
    finally:
        try:
            db.commit()
        except Exception as commit_exc:
            logger.error("Failed to commit JobRun for %s: %s", job_name, commit_exc, exc_info=True)
            try:
                db.rollback()
                run.status = "commit_failed"
                run.error_message = f"Commit failed: {commit_exc}"
                run.finished_at = datetime.now(UTC)
                run.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
                db.commit()
            except Exception:
                logger.error("Failed to update JobRun status after commit failure for %s", job_name)
        finally:
            _release_advisory_lock(lock_conn, job_name)
            db.close()


ORPHANED_RUN_MESSAGE = (
    "Interrupted: the worker process exited before this run finished "
    "(e.g. OOM kill or service restart). Reaped at worker startup."
)


def reap_orphaned_job_runs(db: Any) -> int:
    """Close ``JobRun`` rows left in ``running`` by a worker that died mid-job.

    Every tracked job runs inside the worker process (``app.worker`` is the
    only composition root that registers them), so at worker startup no row
    can legitimately still be running. Without this, an OOM-killed run stays
    ``running`` forever and the job-health views read it as in progress.
    Marks them ``error`` so existing failure counts pick them up.
    """
    from app.foundation.models.entities import JobRun

    now = datetime.now(UTC)
    rows = db.query(JobRun).filter(JobRun.status == "running").all()
    for row in rows:
        row.status = "error"
        row.error_message = ORPHANED_RUN_MESSAGE
        row.finished_at = now
    if rows:
        db.commit()
        logger.warning(
            "Reaped %d orphaned job run(s): %s",
            len(rows), sorted({r.job_name for r in rows}),
        )
    return len(rows)


def _alert_job_failure(db: Any, job_name: str, error_message: str) -> None:
    """Fan a scheduled-job failure out to every user's bell notifications.

    Best-effort: alerting failures must never mask the original job failure
    already recorded on the JobRun row, so every exception here is swallowed.
    """
    try:
        from app.foundation.models.entities import Nudge, User
        from app.foundation.notify import send_notification

        for (user_id,) in db.query(User.id).all():
            nudge = Nudge(
                user_id=user_id,
                source="job_failure",
                severity="warning",
                payload_json=json.dumps({"job_name": job_name, "error": error_message}),
            )
            db.add(nudge)
            db.flush()
            send_notification(db, user_id, "bell", nudge)
        db.commit()
    except Exception as exc:
        logger.error("Failed to alert job failure for %s: %s", job_name, exc, exc_info=True)


def _get_scheduler() -> Any:
    global _scheduler
    if _scheduler is None:
        with _lock:
            if _scheduler is None:
                from apscheduler.schedulers.background import BackgroundScheduler
                s = BackgroundScheduler(job_defaults=scheduler_job_defaults())
                s.start()
                _scheduler = s
    return _scheduler


def submit_job(
    job_fn: Callable,
    args: tuple = (),
    kwargs: dict | None = None,
    job_id: str | None = None,
) -> str:
    """Schedule *job_fn* to run immediately in a background thread.

    Returns the apscheduler job ID.
    """
    jid = job_id or str(uuid.uuid4())
    _get_scheduler().add_job(
        job_fn,
        trigger="date",
        id=jid,
        args=args,
        kwargs=kwargs or {},
        replace_existing=True,
    )
    return jid


def cancel_job(job_id: str) -> bool:
    """Attempt to cancel a pending job. Returns True if found and removed."""
    sched = _get_scheduler()
    job = sched.get_job(job_id)
    if job:
        job.remove()
        return True
    return False


def scheduler_started() -> bool:
    """Return True if this process already has a scheduler, WITHOUT starting one.

    Used by read-only diagnostics that want to peek at job state but must not
    spin up a BackgroundScheduler as a side effect.
    """
    return _scheduler is not None


def get_job_status(job_id: str) -> dict[str, Any]:
    """Return APScheduler metadata for a job if it still exists."""
    sched = _get_scheduler()
    job = sched.get_job(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found_or_completed"}
    return {
        "job_id": job_id,
        "status": "scheduled",
        "next_run": str(job.next_run_time) if job.next_run_time else None,
    }


def register_cron_job(
    job_id: str,
    job_fn: Callable[[], Any],
    *,
    scheduler: Any | None = None,
    track: bool = True,
    hour: int | None = None,
    minute: int | str | None = None,
    second: int | None = None,
    day_of_week: str | None = None,
    day: str | int | None = None,
    month: str | int | None = None,
) -> str:
    """Register a tracked cron job.

    Wraps *job_fn* in ``_track_job`` when *track* is True (default).
    Uses *scheduler* or falls back to the process-global scheduler.
    Pass ``track=False`` for job functions that already call ``_track_job``
    internally (e.g. worker.py job functions).
    """
    if track:
        _fn = job_fn

        def _wrapper() -> None:
            _track_job(job_id, _fn)

        target = _wrapper
    else:
        target = job_fn

    sched = scheduler if scheduler is not None else _get_scheduler()

    trigger: dict[str, Any] = {}
    if hour is not None:
        trigger["hour"] = hour
    if minute is not None:
        trigger["minute"] = minute
    if second is not None:
        trigger["second"] = second
    if day_of_week is not None:
        trigger["day_of_week"] = day_of_week
    if day is not None:
        trigger["day"] = day
    if month is not None:
        trigger["month"] = month

    job = sched.add_job(
        target,
        trigger="cron",
        **trigger,
        id=job_id,
        replace_existing=True,
    )
    return str(job.id)


def register_interval_job(
    job_id: str,
    job_fn: Callable[[], Any],
    *,
    scheduler: Any | None = None,
    track: bool = True,
    minutes: int | None = None,
    hours: int | None = None,
    run_immediately: bool = False,
) -> str:
    """Register a tracked interval job.

    Same semantics as ``register_cron_job`` but uses ``trigger="interval"``.

    By default APScheduler schedules the first execution one full interval
    after registration, not immediately — fine for routine refreshes, but
    for a reconciliation job that must catch up after a restart, that delay
    means anything requiring the job can sit unhandled for up to a full
    interval. Pass ``run_immediately=True`` to fire the first run right away
    (subsequent runs still follow the normal interval).
    """
    if track:
        _fn = job_fn

        def _wrapper() -> None:
            _track_job(job_id, _fn)

        target = _wrapper
    else:
        target = job_fn

    sched = scheduler if scheduler is not None else _get_scheduler()

    trigger: dict[str, Any] = {}
    if minutes is not None:
        trigger["minutes"] = minutes
    if hours is not None:
        trigger["hours"] = hours

    extra: dict[str, Any] = {}
    if run_immediately:
        extra["next_run_time"] = datetime.now(UTC)

    job = sched.add_job(
        target,
        trigger="interval",
        **trigger,
        id=job_id,
        replace_existing=True,
        **extra,
    )
    return str(job.id)


# ---------------------------------------------------------------------------
# Worker heartbeat
# ---------------------------------------------------------------------------
# The scheduler lives in the worker process, so the API cannot ask it what is
# registered. The worker publishes its job list here instead; the Control
# Center reads it to show schedules and next runs, to tell retired job names
# (history only) from live ones, and to know when a "needs worker restart"
# setting has been picked up.

WORKER_HEARTBEAT_KEY = "worker_heartbeat_json"
HEARTBEAT_INTERVAL_MINUTES = 5

_DAY_NAMES = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def describe_trigger(trigger: Any) -> str:
    """A short human schedule for an APScheduler trigger, e.g. 'Sun 08:30 UTC'."""
    interval = getattr(trigger, "interval", None)
    if interval is not None:
        minutes = int(interval.total_seconds() // 60)
        if minutes and minutes % 1440 == 0:
            days = minutes // 1440
            return "Daily" if days == 1 else f"Every {days} days"
        if minutes and minutes % 60 == 0:
            hours = minutes // 60
            return "Hourly" if hours == 1 else f"Every {hours} h"
        return f"Every {minutes} min"
    fields = {field.name: str(field) for field in getattr(trigger, "fields", [])}
    if not fields:
        return str(trigger)
    hour, minute = fields.get("hour", "*"), fields.get("minute", "*")
    at = f"{int(hour):02d}:{int(minute):02d} UTC" if hour.isdigit() and minute.isdigit() else f"{hour}:{minute} UTC"
    day_of_week, day = fields.get("day_of_week", "*"), fields.get("day", "*")
    if day_of_week != "*":
        days = day_of_week
        for short, name in _DAY_NAMES.items():
            days = days.replace(short, name)
        return f"{days.replace('-', '–').replace(',', ', ')} {at}"
    if day != "*":
        return f"Monthly on day {day}, {at}"
    return f"Daily {at}"


def build_worker_heartbeat(scheduler: Any, started_at: datetime) -> dict[str, Any]:
    jobs = []
    for job in scheduler.get_jobs():
        next_run = getattr(job, "next_run_time", None)
        jobs.append({
            "id": job.id,
            "schedule": describe_trigger(job.trigger),
            "next_run": next_run.isoformat() if next_run else None,
        })
    return {
        "started_at": started_at.isoformat(),
        "beat_at": datetime.now(UTC).isoformat(),
        "interval_minutes": HEARTBEAT_INTERVAL_MINUTES,
        "jobs": sorted(jobs, key=lambda job: job["id"]),
    }


def publish_worker_heartbeat(scheduler: Any, started_at: datetime) -> None:
    """Write the worker's registered jobs to app_settings; never raises."""
    from app.foundation.core.db import SessionLocal
    from app.foundation.models.entities import AppSetting

    try:
        payload = json.dumps(build_worker_heartbeat(scheduler, started_at))
        with SessionLocal() as db:
            row = db.query(AppSetting).filter(AppSetting.key == WORKER_HEARTBEAT_KEY).one_or_none()
            if row is None:
                db.add(AppSetting(key=WORKER_HEARTBEAT_KEY, value_json=payload))
            else:
                row.value_json = payload
            db.commit()
    except Exception:
        logger.exception("Worker heartbeat could not be published")
