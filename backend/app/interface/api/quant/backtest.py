"""Backtest API: one timing rule on one instrument, in EUR, judged against luck."""

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.models.entities import BacktestResult, User
from app.lab.quant_lab import run_strategy_backtest, strategy_catalog

router = APIRouter(tags=["quant-backtest"])
logger = logging.getLogger(__name__)


class BacktestRunRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    strategy: str = "trend"
    params: dict[str, float] = Field(default_factory=dict)
    years: int = Field(default=10, ge=2, le=20)
    commission_bps: float = Field(default=10.0, ge=0, le=200)
    spread_bps: float = Field(default=5.0, ge=0, le=200)
    fund_class: Literal["aktien", "misch", "immobilien", "other"] = "other"
    apply_tax: bool = True


@router.get("/backtest/strategies")
def backtest_strategies(_user: User = Depends(current_user)) -> list[dict[str, Any]]:
    return strategy_catalog()


@router.post("/backtest/run")
def backtest_run(
    payload: BacktestRunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Run the rule, record it as a trial and return the three lines with DSR, PBO and a verdict."""
    try:
        result = run_strategy_backtest(
            db, payload.ticker, payload.strategy, payload.params, years=payload.years,
            commission_bps=payload.commission_bps, spread_bps=payload.spread_bps,
            fund_class=payload.fund_class, apply_tax=payload.apply_tax,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result.get("available"):
        summary = {k: v for k, v in result.items() if k not in ("series", "trades")}
        db.add(BacktestResult(
            user_id=user.id, ticker=result["ticker"], strategy=payload.strategy,
            params_json=payload.model_dump_json(), results_json=json.dumps(summary, default=str),
        ))
        db.commit()
    return result


# POST /backtest/strategy and POST /strategies/compare (the single-ticker
# pandas loop, local currency, traded on the signal's own close, no trial
# count) were removed on 2026-10-04; /backtest/run replaces both.
