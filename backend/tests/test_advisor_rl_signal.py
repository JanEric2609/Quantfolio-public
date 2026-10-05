"""Tests for advisor/rl_signal.py — the fail-open read of the latest
validated RL portfolio-allocation signal (Track D2)."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from conftest import _memory_db

from app.foundation.models.entities import QuantRlPolicy, User
from app.decision.advisor.rl_signal import latest_rl_portfolio_signal


def _user(db) -> User:
    user = User(id=str(uuid.uuid4()), username="rl-signal-user", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestLatestRlPortfolioSignal:
    def test_none_when_no_policy_exists(self):
        db = _memory_db()
        user = _user(db)

        assert latest_rl_portfolio_signal(db, user.id) is None

    def test_none_when_only_rejected_policies_exist(self):
        db = _memory_db()
        user = _user(db)
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="portfolio_allocation", algo="ppo",
            status="rejected_below_baseline",
            training_metrics_json=json.dumps({"final_weights": {"AAPL": 0.5}}),
        ))
        db.commit()

        assert latest_rl_portfolio_signal(db, user.id) is None

    def test_returns_weights_from_latest_completed_policy(self):
        db = _memory_db()
        user = _user(db)
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="portfolio_allocation", algo="ppo",
            status="completed",
            training_metrics_json=json.dumps({"final_weights": {"AAPL": 0.6, "MSFT": 0.4}}),
            finished_at=datetime.now(UTC),
        ))
        db.commit()

        signal = latest_rl_portfolio_signal(db, user.id)

        assert signal == {"AAPL": 0.6, "MSFT": 0.4}

    def test_picks_most_recently_finished_completed_policy(self):
        db = _memory_db()
        user = _user(db)
        older = datetime.now(UTC) - timedelta(days=5)
        newer = datetime.now(UTC)
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="portfolio_allocation", algo="ppo",
            status="completed",
            training_metrics_json=json.dumps({"final_weights": {"AAPL": 0.1}}),
            finished_at=older,
        ))
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="portfolio_allocation", algo="ppo",
            status="completed",
            training_metrics_json=json.dumps({"final_weights": {"AAPL": 0.9}}),
            finished_at=newer,
        ))
        db.commit()

        signal = latest_rl_portfolio_signal(db, user.id)

        assert signal == {"AAPL": 0.9}

    def test_none_when_metrics_json_unparsable(self):
        db = _memory_db()
        user = _user(db)
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="portfolio_allocation", algo="ppo",
            status="completed", training_metrics_json="not json",
            finished_at=datetime.now(UTC),
        ))
        db.commit()

        assert latest_rl_portfolio_signal(db, user.id) is None

    def test_none_when_final_weights_missing(self):
        db = _memory_db()
        user = _user(db)
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="portfolio_allocation", algo="ppo",
            status="completed",
            training_metrics_json=json.dumps({"rollout_sharpe": 1.2}),
            finished_at=datetime.now(UTC),
        ))
        db.commit()

        assert latest_rl_portfolio_signal(db, user.id) is None

    def test_scoped_to_env_id(self):
        db = _memory_db()
        user = _user(db)
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="single_stock", algo="ppo",
            status="completed",
            training_metrics_json=json.dumps({"final_weights": {"EUNL.DE": 1.0}}),
            finished_at=datetime.now(UTC),
        ))
        db.commit()

        assert latest_rl_portfolio_signal(db, user.id, env_id="portfolio_allocation") is None
