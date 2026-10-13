"""Data quality and signals API endpoints."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.interface.api.params import LimitQuery

router = APIRouter(tags=["quant-data"])


@router.get("/data-quality/events")
def data_quality_events(
    limit: LimitQuery = 50,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.models.entities import DataQualityEvent
    rows = db.query(DataQualityEvent).order_by(DataQualityEvent.created_at.desc()).limit(limit).all()
    return {
        "events": [
            {
                "id": r.id,
                "provider": r.provider,
                "symbol": r.symbol,
                "severity": r.severity,
                "message": r.message,
                "created_at": str(r.created_at),
            }
            for r in rows
        ]
    }


@router.get("/signals/recent")
def recent_signals(
    limit: LimitQuery = 100,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.models.entities import QuantSignal
    rows = db.query(QuantSignal).order_by(QuantSignal.as_of_date.desc()).limit(limit).all()
    return {
        "signals": [
            {
                "id": r.id,
                "symbol": r.symbol,
                "signal_name": r.signal_name,
                "signal_value": float(r.signal_value) if r.signal_value is not None else None,
                "as_of_date": str(r.as_of_date) if r.as_of_date else None,
            }
            for r in rows
        ]
    }
