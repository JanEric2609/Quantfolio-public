"""Warrants held at a broker: their terms and what their prices imply (ADR 0026).

Read-only towards the brokers: the positions come from the synced mirror, and
the only write is the owner's own record of a warrant's terms.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.derivatives import derivative_positions, terms_dict, upsert_terms
from app.foundation.models.entities import User

router = APIRouter(prefix="/api/derivatives", tags=["derivatives"])

_ISIN = Path(pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class DerivativeTermsIn(BaseModel):
    kind: Literal["put", "call"]
    style: Literal["american", "european"] = "american"
    strike: float = Field(gt=0, lt=1e9)
    strike_currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    # Underlying shares per warrant (Bezugsverhältnis), e.g. 0.1.
    ratio: float = Field(gt=0, le=1000)
    expiry: date
    underlying_symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.^=\-]+$")
    issuer: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class DerivativesOut(BaseModel):
    positions: list[dict[str, Any]]
    estimate: bool = True
    not_financial_advice: bool = True


@router.get("", response_model=DerivativesOut)
def list_derivatives(user: User = Depends(current_user), db: Session = Depends(get_db)) -> DerivativesOut:
    """Every warrant the user's brokers report, with its terms (if entered) and analysis."""
    return DerivativesOut(positions=derivative_positions(db, user.id))


@router.put("/{isin}/terms")
def put_terms(
    body: DerivativeTermsIn,
    isin: str = _ISIN,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record a held warrant's terms (expiry, ratio, strike currency), which the broker feed lacks."""
    held = {p["isin"] for p in derivative_positions(db, user.id)}
    if isin not in held:
        raise HTTPException(status_code=404, detail="No warrant with this ISIN in your synced positions.")
    row = upsert_terms(db, user.id, isin, body.model_dump())
    db.commit()
    db.refresh(row)
    return {"isin": isin, "terms": terms_dict(row)}
