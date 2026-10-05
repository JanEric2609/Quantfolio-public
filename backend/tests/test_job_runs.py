"""Tests for JobRun model and _track_job wrapper."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from conftest import _memory_db
from sqlalchemy import inspect

from app.foundation.models.entities import JobRun


def test_job_run_has_expected_columns():
    db = _memory_db()
    engine = db.get_bind()
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("job_runs")}
    assert "id" in cols
    assert "job_name" in cols
    assert "status" in cols
    assert "started_at" in cols
    assert "finished_at" in cols
    assert "duration_ms" in cols
    assert "error_message" in cols
    assert "triggered_by" in cols


def test_job_run_create_and_read():
    db = _memory_db()

    run = JobRun(
        id=str(uuid.uuid4()),
        job_name="test_job",
        status="running",
        started_at=datetime.now(UTC),
        triggered_by="scheduler",
    )
    db.add(run)
    db.commit()

    fetched = db.query(JobRun).filter(JobRun.job_name == "test_job").first()
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.triggered_by == "scheduler"
    assert fetched.finished_at is None
    assert fetched.duration_ms is None

    db.close()


def test_job_run_update_status():
    db = _memory_db()

    run = JobRun(
        id=str(uuid.uuid4()),
        job_name="test_job",
        status="running",
        started_at=datetime.now(UTC),
        triggered_by="scheduler",
    )
    db.add(run)
    db.commit()

    run.status = "success"
    run.finished_at = datetime.now(UTC)
    run.duration_ms = 1234
    db.commit()

    fetched = db.query(JobRun).filter(JobRun.job_name == "test_job").first()
    assert fetched.status == "success"
    assert fetched.duration_ms == 1234
    assert fetched.finished_at is not None

    db.close()


def test_job_run_error_message():
    db = _memory_db()

    run = JobRun(
        id=str(uuid.uuid4()),
        job_name="test_job",
        status="error",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        duration_ms=500,
        error_message="Something went wrong",
        triggered_by="manual",
    )
    db.add(run)
    db.commit()

    fetched = db.query(JobRun).filter(JobRun.status == "error").first()
    assert fetched is not None
    assert fetched.error_message == "Something went wrong"
    assert fetched.triggered_by == "manual"

    db.close()


def test_track_job_success():
    from unittest.mock import MagicMock, patch

    from app.foundation.jobs import _track_job

    db = _memory_db()
    job_fn = MagicMock()

    with patch("app.foundation.core.db.SessionLocal", return_value=db):
        _track_job("test_job_success", job_fn)

    job_fn.assert_called_once()
    run = db.query(JobRun).filter(JobRun.job_name == "test_job_success").first()
    assert run is not None
    assert run.status == "success"
    assert run.finished_at is not None
    assert run.duration_ms is not None
    db.close()


def test_track_job_error():
    from unittest.mock import MagicMock, patch

    from app.foundation.jobs import _track_job

    db = _memory_db()
    job_fn = MagicMock(side_effect=RuntimeError("boom"))

    with patch("app.foundation.core.db.SessionLocal", return_value=db):
        _track_job("test_job_error", job_fn)

    job_fn.assert_called_once()
    run = db.query(JobRun).filter(JobRun.job_name == "test_job_error").first()
    assert run is not None
    assert run.status == "error"
    assert run.error_message == "boom"
    assert run.finished_at is not None
    assert run.duration_ms is not None
    db.close()


def test_migration_file_exists():
    from pathlib import Path

    migration_path = Path("alembic/versions/0044_job_runs_tracking.py")
    assert migration_path.exists(), f"Migration file not found: {migration_path}"


def test_migration_has_guard_and_down_revision():
    from pathlib import Path

    content = Path("alembic/versions/0044_job_runs_tracking.py").read_text()
    assert 'down_revision = "0043_price_cache_curr"' in content
    assert "has_table" in content
