"""Tests for advisor/rl_training.py — the shared RL training + OOS
validation core (Track D2). Mocks ohlcv_frame/train_policy/run_rollout at
the boundary (the network/SB3/FinRL calls themselves), per AGENTS.md's
"mock only external calls" rule — this module's own fetch-then-gate
orchestration is what's under test."""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

from conftest import _memory_db

from app.foundation.models.entities import QuantRlPolicy, User


def _user(db) -> User:
    user = User(id=str(uuid.uuid4()), username="rl-training-user", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _fake_train_policy_completed(db, policy_id, env_id, *, algo, total_timesteps, model_dir, ohlcv_df):
    row = db.get(QuantRlPolicy, policy_id)
    row.status = "completed"
    row.artefact_path = f"/fake/{policy_id}/policy.zip"
    row.training_metrics_json = json.dumps({"total_timesteps": total_timesteps})
    db.commit()


def _fake_train_policy_failed(db, policy_id, env_id, *, algo, total_timesteps, model_dir, ohlcv_df):
    row = db.get(QuantRlPolicy, policy_id)
    row.status = "failed"
    row.error_message = "training blew up"
    db.commit()


_PATCH_FETCH = "app.foundation.market.ohlcv_frame"
_PATCH_TRAIN = "app.lab.quant_rl.train.train_policy"
_PATCH_ROLLOUT = "app.lab.quant_rl.rollout.run_rollout"


class TestTrainAndValidatePolicy:
    def test_always_persists_a_row(self):
        from app.decision.advisor.rl_training import train_and_validate_policy

        db = _memory_db()
        user = _user(db)

        with (
            patch(_PATCH_FETCH, return_value="fake-df"),
            patch(_PATCH_TRAIN, side_effect=_fake_train_policy_failed),
        ):
            row = train_and_validate_policy(db, user.id)

        assert db.query(QuantRlPolicy).count() == 1
        assert row.status == "failed"

    def test_fetch_failure_never_reaches_training(self):
        from app.decision.advisor.rl_training import train_and_validate_policy

        db = _memory_db()
        user = _user(db)

        with (
            patch(_PATCH_FETCH, side_effect=ValueError("no price history")),
            patch(_PATCH_TRAIN) as mock_train,
        ):
            row = train_and_validate_policy(db, user.id)

        mock_train.assert_not_called()
        assert row.status == "failed"
        assert "OHLCV fetch failed" in row.error_message

    def test_training_failure_never_reaches_rollout(self):
        from app.decision.advisor.rl_training import train_and_validate_policy

        db = _memory_db()
        user = _user(db)

        with (
            patch(_PATCH_FETCH, return_value="fake-df"),
            patch(_PATCH_TRAIN, side_effect=_fake_train_policy_failed),
            patch(_PATCH_ROLLOUT) as mock_rollout,
        ):
            train_and_validate_policy(db, user.id)

        mock_rollout.assert_not_called()

    def test_rejected_when_rollout_sharpe_non_positive(self):
        from app.decision.advisor.rl_training import train_and_validate_policy

        db = _memory_db()
        user = _user(db)
        # Alternating +/-1% returns: mean ~0, Sharpe not clearly positive.
        period_returns = [-0.01 if i % 2 == 0 else 0.01 for i in range(40)]

        with (
            patch(_PATCH_FETCH, return_value="fake-df"),
            patch(_PATCH_TRAIN, side_effect=_fake_train_policy_completed),
            patch(
                _PATCH_ROLLOUT,
                return_value={
                    "status": "completed",
                    "final_value": 1.0,
                    "final_weights": {"AAPL": 0.5, "MSFT": 0.5},
                    "period_returns": period_returns,
                },
            ),
        ):
            row = train_and_validate_policy(db, user.id)

        assert row.status == "rejected_below_baseline"
        metrics = json.loads(row.training_metrics_json)
        assert metrics["rollout_sharpe"] is not None

    def test_completed_when_rollout_sharpe_positive(self):
        from app.decision.advisor.rl_training import train_and_validate_policy

        db = _memory_db()
        user = _user(db)
        # Small positive drift with a touch of noise (constant returns give
        # zero variance -> sharpe_ratio's zero-stdev guard forces Sharpe to
        # 0.0, not a clearly positive number) -> clearly positive Sharpe.
        period_returns = [0.002 if i % 2 == 0 else 0.0005 for i in range(40)]

        with (
            patch(_PATCH_FETCH, return_value="fake-df"),
            patch(_PATCH_TRAIN, side_effect=_fake_train_policy_completed),
            patch(
                _PATCH_ROLLOUT,
                return_value={
                    "status": "completed",
                    "final_value": 1.05,
                    "final_weights": {"AAPL": 0.6, "MSFT": 0.4},
                    "period_returns": period_returns,
                },
            ),
        ):
            row = train_and_validate_policy(db, user.id)

        assert row.status == "completed"
        metrics = json.loads(row.training_metrics_json)
        assert metrics["rollout_sharpe"] > 0
        assert metrics["final_weights"] == {"AAPL": 0.6, "MSFT": 0.4}

    def test_rejected_when_too_few_period_returns(self):
        from app.decision.advisor.rl_training import train_and_validate_policy

        db = _memory_db()
        user = _user(db)

        with (
            patch(_PATCH_FETCH, return_value="fake-df"),
            patch(_PATCH_TRAIN, side_effect=_fake_train_policy_completed),
            patch(
                _PATCH_ROLLOUT,
                return_value={
                    "status": "completed",
                    "final_value": 1.0,
                    "final_weights": {"AAPL": 1.0},
                    "period_returns": [0.001] * 5,
                },
            ),
        ):
            row = train_and_validate_policy(db, user.id)

        assert row.status == "rejected_below_baseline"

    def test_rejected_when_rollout_itself_fails(self):
        from app.decision.advisor.rl_training import train_and_validate_policy

        db = _memory_db()
        user = _user(db)

        with (
            patch(_PATCH_FETCH, return_value="fake-df"),
            patch(_PATCH_TRAIN, side_effect=_fake_train_policy_completed),
            patch(_PATCH_ROLLOUT, return_value={"status": "failed", "message": "no artefact"}),
        ):
            row = train_and_validate_policy(db, user.id)

        assert row.status == "rejected_below_baseline"
