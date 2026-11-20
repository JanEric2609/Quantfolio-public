"""Shared portfolio utility helpers."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.foundation.models.entities import Holding
from app.foundation.valuation import value_holding


def holding_market_value(db: Session, h: Holding) -> float:
    """Return market value of a holding in EUR (latest cached price, else cost basis).

    Thin wrapper delegating to the unified :func:`app.foundation.valuation.value_holding`
    so the price-selection and FX-conversion rules live in exactly one place.
    """
    return value_holding(db, h).converted_value
