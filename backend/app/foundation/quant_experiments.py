"""Quant experiment execution service.

Extracted from ``api/quant.py:run_experiment`` so that the scheduled worker
can invoke the same code path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.foundation.models.entities import QuantExperiment, QuantExperimentRun, StrategyGraveyardEntry
from app.foundation.backtest_strategy import run_strategy_backtest_cached


def run_experiment_once(db: Session, experiment: QuantExperiment) -> QuantExperimentRun:
    """Run a single quant experiment and persist the results.

    Mirrors the logic in ``POST /api/quant/experiments/{experiment_id}/run``.
    """
    universe = json.loads(experiment.universe_json or "[]")
    ticker = universe[0] if universe else experiment.benchmark_symbol or "EUNL.DE"
    result = run_strategy_backtest_cached(
        db,
        ticker,
        "momentum" if experiment.strategy_type == "momentum" else "buy_hold",
        experiment.start_date,
        experiment.end_date,
        json.loads(experiment.config_json or "{}") | {"rebalance_frequency": experiment.rebalance_frequency},
    )
    status = "failed" if result.get("status") != "completed" or not result.get("trades") and experiment.strategy_type != "buy_hold" else "completed"
    run = QuantExperimentRun(
        experiment_id=experiment.id,
        status=status,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        provider_snapshot_json=json.dumps({"ticker": ticker, "source": result.get("source")}),
        metrics_json=json.dumps(result.get("metrics", {})),
        equity_curve_json=json.dumps(result.get("equity_curve", [])),
        trades_json=json.dumps(result.get("trades", [])),
        warnings_json=json.dumps(result.get("warnings", [])),
        error_message=result.get("message") if status == "failed" else None,
    )
    db.add(run)
    if status == "failed":
        db.add(
            StrategyGraveyardEntry(
                user_id=experiment.user_id,
                name=f"{experiment.name} failed run",
                reason=run.error_message or "Strategy had no trades or insufficient data.",
                config_json=experiment.config_json,
                failed_metrics_json=json.dumps(result.get("metrics", {})),
            )
        )
    db.commit()
    db.refresh(run)
    return run
