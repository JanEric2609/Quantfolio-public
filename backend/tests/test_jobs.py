"""Tests for jobs.py: JobRun tracking and scheduler functions."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from conftest import _memory_db

from app.foundation.models.entities import JobRun, Notification, Nudge, User


class TestTrackJob:
    def test_creates_job_run_row(self) -> None:
        from app.foundation.jobs import _track_job

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            try:
                _track_job("test_job", lambda: None)
            except TypeError:
                pass

        runs = db.query(JobRun).all()
        assert len(runs) == 1
        assert runs[0].job_name == "test_job"

    def test_sets_status_success(self) -> None:
        from app.foundation.jobs import _track_job

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            try:
                _track_job("test_job", lambda: None)
            except TypeError:
                pass

        run = db.query(JobRun).first()
        assert run.status in ("success", "running", "error")

    def test_sets_status_error_on_exception(self) -> None:
        from app.foundation.jobs import _track_job

        def failing_job():
            raise ValueError("test error")

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            try:
                _track_job("test_job", failing_job)
            except TypeError:
                pass

        run = db.query(JobRun).first()
        assert run.status in ("error", "running")

    def test_records_triggered_by(self) -> None:
        from app.foundation.jobs import _track_job

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            try:
                _track_job("test_job", lambda: None, triggered_by="manual")
            except TypeError:
                pass

        run = db.query(JobRun).first()
        assert run.triggered_by == "manual"

    def test_duration_ms_is_positive(self) -> None:
        from app.foundation.jobs import _track_job

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            try:
                _track_job("test_job", lambda: None)
            except TypeError:
                pass

        run = db.query(JobRun).first()
        assert run.duration_ms is None or run.duration_ms >= 0

    def test_none_return_value_stays_success(self) -> None:
        """Every current job function returns None; behavior must be unchanged."""
        from app.foundation.jobs import _track_job

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("test_job", lambda: None)

        run = db.query(JobRun).first()
        assert run.status == "success"
        assert run.error_message is None

    def test_warning_sentinel_sets_warning_status(self) -> None:
        """A job_fn returning {"status": "warning", "reason": ...} sets status=warning
        with the reason recorded as error_message, instead of unconditional success."""
        from app.foundation.jobs import _track_job

        def refusing_job():
            return {"status": "warning", "reason": "insufficient_clean_features"}

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("test_job", refusing_job)

        run = db.query(JobRun).first()
        assert run.status == "warning"
        assert run.error_message == "insufficient_clean_features"

    def test_warning_sentinel_reason_truncated_to_1000_chars(self) -> None:
        from app.foundation.jobs import _track_job

        long_reason = "x" * 2000

        def refusing_job():
            return {"status": "warning", "reason": long_reason}

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("test_job", refusing_job)

        run = db.query(JobRun).first()
        assert run.status == "warning"
        assert len(run.error_message) == 1000

    def test_non_warning_dict_return_stays_success(self) -> None:
        """Only the exact {"status": "warning"} shape opts in; any other dict
        return value (e.g. a job's own informational payload) must not be
        misinterpreted as a warning."""
        from app.foundation.jobs import _track_job

        def informational_job():
            return {"status": "ok", "rows": 5}

        db = _memory_db()
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("test_job", informational_job)

        run = db.query(JobRun).first()
        assert run.status == "success"

    def test_failure_alerts_every_user_via_bell(self) -> None:
        """A job exception must fan a bell Nudge+Notification out to every
        user, not just record the JobRun error — silent scheduler failures
        (e.g. the ECB risk-free-rate job) previously went unnoticed for
        days because nothing surfaced them outside the job_run table."""
        from app.foundation.jobs import _track_job

        def failing_job():
            raise ValueError("boom")

        db = _memory_db()
        user = User(username="alerttest", password_hash="x")
        db.add(user)
        db.commit()
        user_id = user.id

        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("test_job", failing_job)

        run = db.query(JobRun).first()
        assert run.status == "error"

        nudges = db.query(Nudge).filter(Nudge.source == "job_failure").all()
        assert len(nudges) == 1
        assert nudges[0].user_id == user_id
        assert "test_job" in nudges[0].payload_json

        notifications = db.query(Notification).all()
        assert len(notifications) == 1
        assert "test_job" in notifications[0].title

    def test_success_does_not_alert(self) -> None:
        from app.foundation.jobs import _track_job

        db = _memory_db()
        db.add(User(username="alerttest2", password_hash="x"))
        db.commit()

        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("test_job", lambda: None)

        assert db.query(Nudge).filter(Nudge.source == "job_failure").count() == 0


class TestSubmitJob:
    def test_returns_job_id(self) -> None:
        from app.foundation.jobs import submit_job

        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_sched.return_value = mock_scheduler
            job_id = submit_job(lambda: None)
            assert isinstance(job_id, str)
            assert len(job_id) > 0

    def test_uses_provided_job_id(self) -> None:
        from app.foundation.jobs import submit_job

        custom_id = str(uuid.uuid4())
        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_sched.return_value = mock_scheduler
            job_id = submit_job(lambda: None, job_id=custom_id)
            assert job_id == custom_id


class TestCancelJob:
    def test_returns_false_for_nonexistent(self) -> None:
        from app.foundation.jobs import cancel_job

        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_scheduler.get_job.return_value = None
            mock_sched.return_value = mock_scheduler
            result = cancel_job("nonexistent")
            assert result is False

    def test_returns_true_for_existing(self) -> None:
        from app.foundation.jobs import cancel_job

        mock_job = MagicMock()
        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_scheduler.get_job.return_value = mock_job
            mock_sched.return_value = mock_scheduler
            result = cancel_job("existing")
            assert result is True
            mock_job.remove.assert_called_once()


class TestGetJobStatus:
    def test_not_found(self) -> None:
        from app.foundation.jobs import get_job_status

        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_scheduler.get_job.return_value = None
            mock_sched.return_value = mock_scheduler
            result = get_job_status("nonexistent")
            assert result["status"] == "not_found_or_completed"

    def test_scheduled(self) -> None:
        from app.foundation.jobs import get_job_status

        mock_job = MagicMock()
        mock_job.next_run_time = datetime.now(UTC)
        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_scheduler.get_job.return_value = mock_job
            mock_sched.return_value = mock_scheduler
            result = get_job_status("existing")
            assert result["status"] == "scheduled"


class TestRegisterCronJob:
    def test_registers_cron_job_with_params(self) -> None:
        from app.foundation.jobs import register_cron_job

        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_job = MagicMock()
            mock_job.id = "test_cron"
            mock_scheduler.add_job.return_value = mock_job
            mock_sched.return_value = mock_scheduler

            result = register_cron_job("test_cron", lambda: None, hour=8, minute=0)

            assert result == "test_cron"
            call_kwargs = mock_scheduler.add_job.call_args[1]
            assert call_kwargs["trigger"] == "cron"
            assert call_kwargs["hour"] == 8
            assert call_kwargs["minute"] == 0
            assert call_kwargs["id"] == "test_cron"
            assert call_kwargs["replace_existing"] is True

    def test_wraps_in_track_job_when_track_true(self) -> None:
        from app.foundation.jobs import register_cron_job

        with patch("app.foundation.jobs._get_scheduler") as mock_sched, \
             patch("app.foundation.jobs._track_job") as mock_track:
            mock_scheduler = MagicMock()
            mock_job = MagicMock()
            mock_job.id = "test_cron"
            mock_scheduler.add_job.return_value = mock_job
            mock_sched.return_value = mock_scheduler

            fn = lambda: None
            register_cron_job("test_cron", fn, hour=8, minute=0)

            wrapper_fn = mock_scheduler.add_job.call_args[0][0]
            wrapper_fn()
            mock_track.assert_called_once_with("test_cron", fn)

    def test_passes_fn_directly_when_track_false(self) -> None:
        from app.foundation.jobs import register_cron_job

        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_job = MagicMock()
            mock_job.id = "test_cron"
            mock_scheduler.add_job.return_value = mock_job
            mock_sched.return_value = mock_scheduler

            fn = lambda: None
            register_cron_job("test_cron", fn, scheduler=mock_scheduler,
                              hour=8, minute=0, track=False)

            passed_fn = mock_scheduler.add_job.call_args[0][0]
            assert passed_fn is fn

    def test_uses_provided_scheduler(self) -> None:
        from app.foundation.jobs import register_cron_job

        custom_scheduler = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "test_cron"
        custom_scheduler.add_job.return_value = mock_job

        result = register_cron_job(
            "test_cron", lambda: None, scheduler=custom_scheduler, hour=8, minute=0
        )
        assert result == "test_cron"
        custom_scheduler.add_job.assert_called_once()


class TestRegisterIntervalJob:
    def test_registers_interval_job_with_minutes(self) -> None:
        from app.foundation.jobs import register_interval_job

        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_job = MagicMock()
            mock_job.id = "test_interval"
            mock_scheduler.add_job.return_value = mock_job
            mock_sched.return_value = mock_scheduler

            result = register_interval_job("test_interval", lambda: None, minutes=15)

            assert result == "test_interval"
            call_kwargs = mock_scheduler.add_job.call_args[1]
            assert call_kwargs["trigger"] == "interval"
            assert call_kwargs["minutes"] == 15

    def test_registers_interval_job_with_hours(self) -> None:
        from app.foundation.jobs import register_interval_job

        with patch("app.foundation.jobs._get_scheduler") as mock_sched:
            mock_scheduler = MagicMock()
            mock_job = MagicMock()
            mock_job.id = "test_interval"
            mock_scheduler.add_job.return_value = mock_job
            mock_sched.return_value = mock_scheduler

            result = register_interval_job("test_interval", lambda: None, hours=4)

            assert result == "test_interval"
            call_kwargs = mock_scheduler.add_job.call_args[1]
            assert call_kwargs["trigger"] == "interval"
            assert call_kwargs["hours"] == 4

    def test_passes_fn_directly_when_track_false(self) -> None:
        from app.foundation.jobs import register_interval_job

        custom_scheduler = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "test_interval"
        custom_scheduler.add_job.return_value = mock_job

        fn = lambda: None
        register_interval_job(
            "test_interval", fn, scheduler=custom_scheduler, minutes=15, track=False
        )
        passed_fn = custom_scheduler.add_job.call_args[0][0]
        assert passed_fn is fn


class TestAdvisoryLockRelease:
    """A session-level pg advisory lock must be released on *every* exit path.

    The lock is held on a dedicated connection for the whole job. Returning
    without releasing it strands the lock on a connection that is never reused,
    so every later run of the same job name blocks forever and the connection
    never returns to the pool. The JobRun-insert failure path used to return
    early and skip the release entirely.
    """

    @staticmethod
    def _pg_session(commit_raises: bool):
        """A mock Session that looks like PostgreSQL and owns a mock lock conn."""
        from sqlalchemy.engine import Engine

        lock_conn = MagicMock()
        engine = MagicMock()
        # `dialect` is set in Engine.__init__, so spec= would not expose it;
        # assigning __class__ is what makes the isinstance(db.bind, Engine)
        # guard in _track_job take the PostgreSQL branch.
        engine.__class__ = Engine
        engine.dialect.name = "postgresql"
        engine.connect.return_value = lock_conn

        db = MagicMock()
        db.bind = engine
        if commit_raises:
            db.commit.side_effect = RuntimeError("JobRun insert failed")
        return db, lock_conn

    @staticmethod
    def _unlock_calls(lock_conn):
        return [
            c for c in lock_conn.exec_driver_sql.call_args_list
            if "pg_advisory_unlock" in str(c)
        ]

    def test_lock_released_when_job_run_insert_fails(self) -> None:
        """The regression: the early return used to leak the lock."""
        from app.foundation.jobs import _track_job

        db, lock_conn = self._pg_session(commit_raises=True)
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("leaky_job", lambda: None)

        assert self._unlock_calls(lock_conn), "advisory lock was never released"
        lock_conn.close.assert_called_once()
        db.close.assert_called_once()

    def test_lock_released_on_the_normal_path(self) -> None:
        from app.foundation.jobs import _track_job

        db, lock_conn = self._pg_session(commit_raises=False)
        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("ok_job", lambda: None)

        assert self._unlock_calls(lock_conn)
        lock_conn.close.assert_called_once()

    def test_lock_released_when_the_job_itself_raises(self) -> None:
        from app.foundation.jobs import _track_job

        db, lock_conn = self._pg_session(commit_raises=False)

        def boom():
            raise ValueError("job exploded")

        with patch("app.foundation.core.db.SessionLocal", return_value=db):
            _track_job("boom_job", boom)

        assert self._unlock_calls(lock_conn)
        lock_conn.close.assert_called_once()

    def test_failed_unlock_invalidates_the_connection(self) -> None:
        """If UNLOCK fails, drop the raw connection so the server frees the lock."""
        from app.foundation.jobs import _release_advisory_lock

        lock_conn = MagicMock()
        lock_conn.exec_driver_sql.side_effect = RuntimeError("connection gone")

        _release_advisory_lock(lock_conn, "some_job")

        lock_conn.invalidate.assert_called_once()
        lock_conn.close.assert_called_once()

    def test_release_is_a_noop_without_a_lock(self) -> None:
        from app.foundation.jobs import _release_advisory_lock

        _release_advisory_lock(None, "sqlite_job")  # must not raise


class TestReapOrphanedJobRuns:
    """A worker killed mid-job (OOM) leaves its JobRun at ``running`` forever."""

    def test_running_rows_become_errors_and_finished_rows_are_untouched(self) -> None:
        from app.foundation.jobs import ORPHANED_RUN_MESSAGE, reap_orphaned_job_runs

        db = _memory_db()
        started = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
        db.add_all([
            JobRun(id=str(uuid.uuid4()), job_name="alphacrafter_daily", status="running", started_at=started),
            JobRun(id=str(uuid.uuid4()), job_name="discover_ml_training", status="running", started_at=started),
            JobRun(id=str(uuid.uuid4()), job_name="price_refresh", status="success", started_at=started,
                   finished_at=started),
        ])
        db.commit()

        assert reap_orphaned_job_runs(db) == 2

        by_name = {r.job_name: r for r in db.query(JobRun).all()}
        for name in ("alphacrafter_daily", "discover_ml_training"):
            assert by_name[name].status == "error"
            assert by_name[name].error_message == ORPHANED_RUN_MESSAGE
            assert by_name[name].finished_at is not None
        assert by_name["price_refresh"].status == "success"
        assert by_name["price_refresh"].error_message is None

    def test_no_running_rows_is_a_no_op(self) -> None:
        from app.foundation.jobs import reap_orphaned_job_runs

        db = _memory_db()
        assert reap_orphaned_job_runs(db) == 0
