"""Experiments API endpoints — experiment CRUD, runs, graveyard."""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.interface.api.quant._common import _experiment_to_dict, _graveyard_to_dict, _run_to_dict
from app.foundation.core.db import get_db
from app.foundation.models.entities import QuantExperiment, QuantExperimentRun, User
from app.foundation.schemas import GraveyardIn, QuantExperimentIn
from app.foundation.auth import current_user

router = APIRouter(tags=["quant-experiments"])


@router.get("/experiments")
def list_experiments(limit: int = 200, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    rows = db.query(QuantExperiment).filter(QuantExperiment.user_id == user.id).order_by(QuantExperiment.created_at.desc()).limit(max(1, min(limit, 1000))).all()
    return [_experiment_to_dict(row) for row in rows]


@router.get("/runs/recent")
def list_recent_runs(
    limit: int = 50, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    """Unified, chronologically-sorted run history."""
    from app.foundation.models.entities import BacktestResult

    runs: list[dict[str, Any]] = []

    def _num(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # Experiment runs
    exp_rows = (
        db.query(QuantExperimentRun, QuantExperiment)
        .join(QuantExperiment, QuantExperimentRun.experiment_id == QuantExperiment.id)
        .filter(QuantExperiment.user_id == user.id)
        .order_by(QuantExperimentRun.started_at.desc())
        .limit(limit)
        .all()
    )
    for run, exp in exp_rows:
        metrics = json.loads(run.metrics_json or "{}")
        ts = run.finished_at or run.started_at
        runs.append({
            "id": run.id,
            "type": "experiment",
            "title": exp.name,
            "subtitle": exp.strategy_type or exp.mode or "experiment",
            "status": run.status,
            "metric_label": "Sharpe",
            "metric_value": _num(metrics.get("sharpe")),
            "created_at": ts.isoformat() if ts else None,
            "ref": exp.id,
        })

    # Strategy backtests (Track C2)
    backtest_rows = (
        db.query(BacktestResult)
        .filter(BacktestResult.user_id == user.id)
        .order_by(BacktestResult.run_at.desc())
        .limit(limit)
        .all()
    )
    for b in backtest_rows:
        try:
            metrics = json.loads(b.results_json or "{}").get("metrics", {})
        except (json.JSONDecodeError, TypeError):
            metrics = {}
        runs.append({
            "id": b.id,
            "type": "backtest",
            "title": f"{b.ticker} — {b.strategy}",
            "subtitle": "strategy backtest",
            "status": "completed",
            "metric_label": "Sharpe",
            "metric_value": _num(metrics.get("sharpe")),
            "created_at": b.run_at.isoformat() if b.run_at else None,
            "ref": b.id,
        })

    runs.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return {"runs": runs[:limit]}


@router.post("/experiments")
def create_experiment(payload: QuantExperimentIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    row = QuantExperiment(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        mode=payload.mode,
        universe_json=json.dumps(payload.universe),
        benchmark_symbol=payload.benchmark_symbol,
        start_date=payload.start_date,
        end_date=payload.end_date,
        rebalance_frequency=payload.rebalance_frequency,
        strategy_type=payload.strategy_type,
        config_json=json.dumps(payload.config),
        active=payload.active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _experiment_to_dict(row)


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    row = db.get(QuantExperiment, experiment_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return _experiment_to_dict(row)


@router.post("/experiments/{experiment_id}/run")
def run_experiment(experiment_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    from app.foundation.quant_experiments import run_experiment_once
    experiment = db.get(QuantExperiment, experiment_id)
    if experiment is None or experiment.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    run = run_experiment_once(db, experiment)
    return _run_to_dict(run)


@router.get("/experiments/{experiment_id}/runs")
def experiment_runs(experiment_id: str, limit: int = 200, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    experiment = db.get(QuantExperiment, experiment_id)
    if experiment is None or experiment.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    rows = db.query(QuantExperimentRun).filter(QuantExperimentRun.experiment_id == experiment_id).order_by(QuantExperimentRun.started_at.desc()).limit(max(1, min(limit, 1000))).all()
    return [_run_to_dict(row) for row in rows]


@router.post("/graveyard")
def create_graveyard_entry(payload: GraveyardIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    from app.foundation.models.entities import StrategyGraveyardEntry
    row = StrategyGraveyardEntry(
        user_id=user.id,
        name=payload.name,
        reason=payload.reason,
        config_json=json.dumps(payload.config),
        failed_metrics_json=json.dumps(payload.failed_metrics),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _graveyard_to_dict(row)


@router.get("/graveyard")
def strategy_graveyard(limit: int = 200, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    from app.foundation.models.entities import StrategyGraveyardEntry
    rows = db.query(StrategyGraveyardEntry).filter(StrategyGraveyardEntry.user_id == user.id).order_by(StrategyGraveyardEntry.created_at.desc()).limit(max(1, min(limit, 1000))).all()
    return [_graveyard_to_dict(row) for row in rows]
