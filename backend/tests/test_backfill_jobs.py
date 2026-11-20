"""Tests for background price-backfill jobs (proposal P4).

Long backfills previously ran inline on the request. These cover the durable
BackfillJob row + service: enqueue creates a queued job, execution records
success/failure, and status is scoped to the owning user.
"""
from conftest import _memory_db

from app.foundation.models.entities import BackfillJob
from app.foundation import backfill_jobs


def test_enqueue_creates_queued_job(monkeypatch):
    db = _memory_db()
    submitted = {}
    # Phase E: backfill_jobs imports submit_job lazily from the generic infra,
    # so the stub patches the source module.
    monkeypatch.setattr(
        "app.foundation.jobs.submit_job",
        lambda fn, args=(), kwargs=None, job_id=None: submitted.update(kwargs or {}) or "sched-1",
    )

    job_id = backfill_jobs.enqueue_backfill(db, "user-1", days=900)

    job = db.get(BackfillJob, job_id)
    assert job is not None
    assert job.status == "queued"
    assert job.days == 900
    assert job.user_id == "user-1"
    # The scheduled callable was handed this job's id.
    assert submitted.get("job_id") == job_id


def test_execute_backfill_records_success(monkeypatch):
    db = _memory_db()
    job_id = backfill_jobs._create_job(db, "user-1", days=1000)

    fake = {"total": 3, "succeeded": 3, "failed": 0, "results": [{"symbol": "AAPL", "ok": True}]}
    monkeypatch.setattr(backfill_jobs, "backfill_user_prices", lambda db, uid, days: fake)

    backfill_jobs._execute_backfill(db, job_id)

    job = db.get(BackfillJob, job_id)
    assert job.status == "succeeded"
    assert job.total == 3
    assert job.succeeded == 3
    assert job.failed == 0
    assert job.finished_at is not None


def test_execute_backfill_records_failure(monkeypatch):
    db = _memory_db()
    job_id = backfill_jobs._create_job(db, "user-1", days=1000)

    def _boom(db, uid, days):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(backfill_jobs, "backfill_user_prices", _boom)

    backfill_jobs._execute_backfill(db, job_id)

    job = db.get(BackfillJob, job_id)
    assert job.status == "failed"
    assert "provider exploded" in (job.error_message or "")
    assert job.finished_at is not None


def test_get_backfill_status_scoped_to_user():
    db = _memory_db()
    job_id = backfill_jobs._create_job(db, "owner", days=500)

    assert backfill_jobs.get_backfill_status(db, job_id, "someone-else") is None
    status = backfill_jobs.get_backfill_status(db, job_id, "owner")
    assert status is not None
    assert status["job_id"] == job_id
    assert status["status"] == "queued"
    assert status["days"] == 500
