"""Tests for advisor/jobs.py::register_advisor_rl_training_job's inner
logic (Track D2): skipped entirely when FinRL is disabled, skips a user
already retrained within the cooldown window, and trains everyone else via
advisor.rl_training.train_and_validate_policy."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from conftest import _memory_db

from app.foundation.models.entities import QuantRlPolicy, User
from app.decision.advisor.jobs import register_advisor_rl_training_job


class _CapturingScheduler:
    """Grabs the job function passed to add_job without running it."""

    def __init__(self):
        self.fn = None

    def add_job(self, fn, **kwargs):
        self.fn = fn

        class _Handle:
            id = kwargs["id"]

        return _Handle()


def _inner_fn():
    sched = _CapturingScheduler()
    register_advisor_rl_training_job(sched)
    return sched.fn


def _user(db, name="rl-job-user") -> User:
    user = User(id=str(uuid.uuid4()), username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestAdvisorRlTrainingJob:
    def test_skipped_entirely_when_finrl_disabled(self):
        inner = _inner_fn()

        with (
            patch("app.foundation.core.config.get_settings", return_value=SimpleNamespace(finrl_enabled=False)),
            patch("app.decision.advisor.rl_training.train_and_validate_policy") as mock_train,
        ):
            inner()

        mock_train.assert_not_called()

    def test_trains_a_user_with_no_recent_policy(self):
        db = _memory_db()
        user = _user(db)
        inner = _inner_fn()

        with (
            patch("app.foundation.core.config.get_settings", return_value=SimpleNamespace(finrl_enabled=True)),
            patch("app.foundation.core.db.SessionLocal", return_value=db),
            patch(
                "app.decision.advisor.rl_training.train_and_validate_policy",
                return_value=QuantRlPolicy(
                    id=str(uuid.uuid4()), user_id=user.id, env_id="portfolio_allocation",
                    algo="ppo", status="completed",
                ),
            ) as mock_train,
        ):
            inner()

        mock_train.assert_called_once_with(db, user.id)

    def test_skips_a_user_retrained_within_cooldown(self):
        db = _memory_db()
        user = _user(db)
        db.add(QuantRlPolicy(
            id=str(uuid.uuid4()), user_id=user.id, env_id="portfolio_allocation",
            algo="ppo", status="completed",
            created_at=datetime.now(UTC) - timedelta(days=10),
        ))
        db.commit()
        inner = _inner_fn()

        with (
            patch("app.foundation.core.config.get_settings", return_value=SimpleNamespace(finrl_enabled=True)),
            patch("app.foundation.core.db.SessionLocal", return_value=db),
            patch("app.decision.advisor.rl_training.train_and_validate_policy") as mock_train,
        ):
            inner()

        mock_train.assert_not_called()

    def test_retrains_a_user_past_the_cooldown_window(self):
        db = _memory_db()
        user = _user(db)
        db.add(QuantRlPolicy(
            id=str(uuid.uuid4()), user_id=user.id, env_id="portfolio_allocation",
            algo="ppo", status="completed",
            created_at=datetime.now(UTC) - timedelta(days=200),
        ))
        db.commit()
        inner = _inner_fn()

        with (
            patch("app.foundation.core.config.get_settings", return_value=SimpleNamespace(finrl_enabled=True)),
            patch("app.foundation.core.db.SessionLocal", return_value=db),
            patch(
                "app.decision.advisor.rl_training.train_and_validate_policy",
                return_value=QuantRlPolicy(
                    id=str(uuid.uuid4()), user_id=user.id, env_id="portfolio_allocation",
                    algo="ppo", status="completed",
                ),
            ) as mock_train,
        ):
            inner()

        mock_train.assert_called_once_with(db, user.id)
