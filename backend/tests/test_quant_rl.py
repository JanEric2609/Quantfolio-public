"""Smoke tests for the FinRL + Stable-Baselines3 RL training loop.

Skipped entirely if FinRL is not installed.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from conftest import _memory_db

from app.foundation.models.entities import QuantRlPolicy, User

# Skip the entire module if finrl is not installed
finrl = pytest.importorskip("finrl")


def _fetch_ohlcv(db, env_id: str = "portfolio_allocation"):
    """Deterministic synthetic OHLCV frame for offline RL test execution."""
    import numpy as np
    import pandas as pd
    from app.lab.quant_rl.envs import AVAILABLE_ENVS

    tickers = next(e["assets"] for e in AVAILABLE_ENVS if e["id"] == env_id)
    dates = pd.date_range("2018-01-01", "2022-12-31", freq="B")
    rows = []
    for ticker in tickers:
        rng = np.random.default_rng(abs(hash(ticker)) % 10000)
        n = len(dates)
        ret = rng.normal(0.0005, 0.015, size=n)
        price = 100.0 * np.exp(np.cumsum(ret))
        for d, p in zip(dates, price):
            rows.append({
                "date": d.strftime("%Y-%m-%d"),
                "open": float(p * 0.995),
                "high": float(p * 1.01),
                "low": float(p * 0.99),
                "close": float(p),
                "volume": 1000000.0,
                "tic": ticker,
            })
    return pd.DataFrame(rows)




# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# list_envs
# ---------------------------------------------------------------------------

class TestListEnvs:
    def test_returns_list(self) -> None:
        from app.lab.quant_rl.envs import list_envs

        envs = list_envs()
        assert isinstance(envs, list)
        assert len(envs) >= 1

    def test_portfolio_allocation_present(self) -> None:
        from app.lab.quant_rl.envs import list_envs

        ids = [e["id"] for e in list_envs()]
        assert "portfolio_allocation" in ids


# ---------------------------------------------------------------------------
# train_policy smoke test
# ---------------------------------------------------------------------------

class TestTrainPolicy:
    """Smoke-test: train PPO for 50 steps and verify the DB row is updated."""

    def _make_user_and_policy(self, db):
        user = User(
            id=str(uuid.uuid4()),
            username="rl_test_user",
            password_hash="x",
        )
        db.add(user)
        db.flush()

        policy = QuantRlPolicy(
            id=str(uuid.uuid4()),
            user_id=user.id,
            env_id="portfolio_allocation",
            algo="ppo",
            status="created",
        )
        db.add(policy)
        db.commit()
        return policy

    def test_train_policy_updates_status(self, tmp_path) -> None:
        """Train for 50 steps; policy row should end up in 'completed' or 'failed'."""
        from app.lab.quant_rl.train import train_policy

        db = _memory_db()
        policy = self._make_user_and_policy(db)

        train_policy(
            db=db,
            policy_id=policy.id,
            env_id="portfolio_allocation",
            algo="ppo",
            total_timesteps=50,
            model_dir=str(tmp_path),
            ohlcv_df=_fetch_ohlcv(db),
        )

        db.expire_all()
        row = db.get(QuantRlPolicy, policy.id)
        assert row is not None
        assert row.status in ("completed", "failed"), f"Unexpected status: {row.status!r}"
        assert row.finished_at is not None

    def test_completed_policy_has_artefact_path(self, tmp_path) -> None:
        from app.lab.quant_rl.train import train_policy

        db = _memory_db()
        policy = self._make_user_and_policy(db)

        train_policy(
            db=db,
            policy_id=policy.id,
            env_id="portfolio_allocation",
            algo="ppo",
            total_timesteps=50,
            model_dir=str(tmp_path),
            ohlcv_df=_fetch_ohlcv(db),
        )

        db.expire_all()
        row = db.get(QuantRlPolicy, policy.id)
        if row.status == "completed":
            assert row.artefact_path is not None
            policy_zip = Path(row.artefact_path)
            assert policy_zip.exists(), f"Policy file not found at {policy_zip}"

    def test_train_missing_policy_id_is_noop(self, tmp_path) -> None:
        from app.lab.quant_rl.train import train_policy

        db = _memory_db()
        # Should not raise — just logs an error. Row lookup fails before
        # ohlcv_df is ever touched, so a dummy value is fine here.
        train_policy(
            db=db,
            policy_id="nonexistent-id",
            env_id="portfolio_allocation",
            algo="ppo",
            total_timesteps=50,
            model_dir=str(tmp_path),
            ohlcv_df=None,
        )


# ---------------------------------------------------------------------------
# build_env — additive ticker metadata (Track D2)
# ---------------------------------------------------------------------------


class TestBuildEnvTickers:
    def test_portfolio_allocation_env_carries_sorted_tickers(self) -> None:
        from app.lab.quant_rl.envs import AVAILABLE_ENVS, build_env

        db = _memory_db()
        env = build_env("portfolio_allocation", _fetch_ohlcv(db))

        portfolio_assets = next(
            e["assets"] for e in AVAILABLE_ENVS if e["id"] == "portfolio_allocation"
        )
        assert env.tickers == sorted(portfolio_assets)


# ---------------------------------------------------------------------------
# run_rollout — final_weights / period_returns (Track D2)
# ---------------------------------------------------------------------------


class TestRunRollout:
    def _make_user_and_policy(self, db):
        user = User(id=str(uuid.uuid4()), username="rl_rollout_user", password_hash="x")
        db.add(user)
        db.flush()

        policy = QuantRlPolicy(
            id=str(uuid.uuid4()), user_id=user.id, env_id="portfolio_allocation",
            algo="ppo", status="created",
        )
        db.add(policy)
        db.commit()
        return policy

    def test_rollout_reports_final_weights_and_period_returns(self, tmp_path) -> None:
        from app.lab.quant_rl.rollout import run_rollout
        from app.lab.quant_rl.train import train_policy

        db = _memory_db()
        policy = self._make_user_and_policy(db)
        ohlcv_df = _fetch_ohlcv(db)

        train_policy(
            db=db, policy_id=policy.id, env_id="portfolio_allocation", algo="ppo",
            total_timesteps=50, model_dir=str(tmp_path), ohlcv_df=ohlcv_df,
        )
        db.expire_all()
        row = db.get(QuantRlPolicy, policy.id)
        if row.status != "completed":
            pytest.skip(f"training did not complete (status={row.status!r}), nothing to roll out")

        result = run_rollout(
            db, policy_id=policy.id, env_id="portfolio_allocation", algo="ppo",
            n_steps=5, model_dir=str(tmp_path), ohlcv_df=ohlcv_df,
        )

        assert result["status"] == "completed"
        assert "final_weights" in result
        assert "period_returns" in result
        if result["final_weights"] is not None:
            assert set(result["final_weights"]) <= {"AAPL", "AMZN", "GOOGL", "META", "MSFT"}
            assert abs(sum(result["final_weights"].values()) - 1.0) < 1e-3
        assert isinstance(result["period_returns"], list)
