"""Unified runs feed (/api/quant/runs/recent) merges experiments and backtests."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import _memory_db

from app.interface.api.quant import list_recent_runs
from app.foundation.models.entities import (
    BacktestResult,
    QuantExperiment,
    QuantExperimentRun,
    User,
)


def _user(db) -> User:
    user = User(username="quant", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_recent_runs_merges_remaining_sources_sorted():
    db = _memory_db()
    user = _user(db)
    now = datetime.now(UTC)

    # Experiment + run (oldest)
    exp = QuantExperiment(user_id=user.id, name="Momentum sweep", mode="backtest", strategy_type="momentum")
    db.add(exp)
    db.commit()
    db.refresh(exp)
    db.add(
        QuantExperimentRun(
            experiment_id=exp.id,
            status="completed",
            started_at=now - timedelta(days=2),
            finished_at=now - timedelta(days=2),
            metrics_json=json.dumps({"sharpe": 1.23}),
        )
    )

    # Strategy backtest (newest)
    db.add(
        BacktestResult(
            user_id=user.id,
            ticker="AAPL",
            strategy="sma_crossover",
            params_json="{}",
            results_json=json.dumps({"metrics": {"sharpe": 0.85}}),
            run_at=now - timedelta(days=1),
        )
    )
    db.commit()

    result = list_recent_runs(limit=50, db=db, user=user)
    runs = result["runs"]

    assert {r["type"] for r in runs} == {"experiment", "backtest"}
    # Newest first.
    assert runs[0]["type"] == "backtest"
    assert runs[-1]["type"] == "experiment"
    experiment = next(r for r in runs if r["type"] == "experiment")
    assert experiment["title"] == "Momentum sweep"
    assert experiment["metric_value"] == 1.23
    backtest = next(r for r in runs if r["type"] == "backtest")
    assert backtest["title"] == "AAPL — sma_crossover"
    assert backtest["metric_value"] == 0.85


def test_recent_runs_empty_for_new_user():
    db = _memory_db()
    user = _user(db)
    assert list_recent_runs(limit=50, db=db, user=user) == {"runs": []}
