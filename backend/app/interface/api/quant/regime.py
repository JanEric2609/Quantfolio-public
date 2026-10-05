"""Regime API endpoints — current regime, factor weights, regime gate."""

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.lab.regime import get_or_refresh_regime

router = APIRouter(tags=["quant-regime"])


@router.get("/regime/current")
def current_regime(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    return get_or_refresh_regime(db)


@router.get("/regime/factor-weights")
def regime_factor_weights(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    from app.lab.regime.gate import get_regime_adjusted_weights

    try:
        return get_regime_adjusted_weights(db)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Regime data unavailable: {exc}")


@router.post("/regime/apply-gate")
def apply_regime_gate(
    factor_scores: dict[str, float],
    regime_label: str | None = Query(None, description="Override regime label (default: current)"),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.lab.regime.gate import apply_regime_gate as _apply_gate, get_regime_adjusted_weights

    if regime_label is None:
        regime_info = get_regime_adjusted_weights(db)
        regime_label = cast(str, regime_info["regime_label"])

    return _apply_gate(factor_scores, regime_label)


@router.get("/regime/affect")
def regime_affect(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    from app.lab.regime.gate import RegimeGate

    gate = RegimeGate(db)
    ctx = gate.context()
    return {
        "regime_label": ctx.label,
        "crisis": ctx.crisis,
        "score": ctx.confidence,
        "vix": ctx.vix,
        "yield_spread": ctx.yield_spread,
        "momentum_3m": ctx.momentum_3m,
        "factor_weights": ctx.factor_weights,
        "optimizer_constraints": ctx.constraints,
        "backtest_params": ctx.backtest_params,
        "ml_features": ctx.regime_features(),
    }
