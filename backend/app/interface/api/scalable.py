"""Scalable Capital — read-only sync through the official ``sc`` CLI.

No endpoint here can trade or change anything at Scalable: the service only
runs allow-listed read commands (see ``foundation/scalable/cli.py``). The
login endpoints start ``sc login --local-read-only``, the one non-read argv the
wrapper accepts; the session it creates refuses every mutation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.auth import current_user
from app.foundation.broker_status import owner_user_id
from app.foundation.broker_status import status as scalable_status
from app.foundation.core.db import get_db
from app.foundation.live_positions import broker_positions
from app.foundation.models.entities import User
from app.foundation.scalable import login as scalable_login
from app.foundation.scalable import service as scalable
from app.foundation.scalable.cli import SC_BINARY, ScalableError, ScalableLoginRequired, binary_sha256
from app.foundation.settings import upsert_public_settings

router = APIRouter(prefix="/api/scalable", tags=["scalable"])


class ScalableStatusOut(BaseModel):
    source: str
    enabled: bool
    state: str
    message: str = ""
    error_code: str | None = None
    last_sync_at: datetime | None = None
    last_success_at: datetime | None = None
    stale: bool = False
    portfolio_id: str | None = None
    tracking_since: str | None = None
    positions: int = 0
    pending_reconciliation: int = 0
    read_only: bool = True


class ScalableSyncLogOut(BaseModel):
    id: str
    source: str
    trigger: str
    state: str
    error_code: str | None = None
    message: str = ""
    counts: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ScalablePositionOut(BaseModel):
    id: str
    isin: str
    ticker: str | None = None
    name: str
    security_type: str | None = None
    quantity: float
    pending_quantity: float | None = None
    avg_buy_price: float | None = None
    current_price: float | None = None
    current_value: float | None = None
    currency: str
    price_timestamp: datetime | None = None
    price_outdated: bool = False
    last_synced: datetime | None = None


class ScalableSavingsPlanOut(BaseModel):
    isin: str | None = None
    name: str = ""
    amount: str | None = None
    frequency: str | None = None
    day_of_month: int | None = None
    next_execution_date: str | None = None
    kind: str = "security"


class ScalableReconcileRowOut(BaseModel):
    holding_id: str
    isin: str
    name: str
    manual_quantity: str
    manual_value: str
    scalable_quantity: str
    scalable_value: str | None = None
    decision: str | None = None
    quantity_matches: bool


class ScalableReconcileIn(BaseModel):
    # manual holding id -> decision
    decisions: dict[str, Literal["replace", "keep_both"]]


class ScalableReconcileResultOut(BaseModel):
    replaced: int
    kept: int


class ScalableProbeStepOut(BaseModel):
    name: str
    ok: bool
    detail: str


class ScalableProbeOut(BaseModel):
    ok: bool
    message: str
    steps: list[ScalableProbeStepOut] = Field(default_factory=list)
    code: str | None = None
    hints: list[str] = Field(default_factory=list)


class ScalableLoginOut(BaseModel):
    state: str
    verification_url: str | None = None
    verification_host: str | None = None
    user_code: str | None = None
    message: str | None = None
    error_code: str | None = None
    read_only_confirmed: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ScalablePinOut(BaseModel):
    sha256: str
    pinned: bool


def require_connection_owner(db: Session, user: User) -> None:
    """The sc login is server-wide: only the user it syncs into (or, before the
    first sync, an admin) may replace it or pin its binary."""
    owner = owner_user_id(db)
    if owner is not None and owner != user.id:
        raise HTTPException(status_code=403, detail="Only the user Scalable Capital syncs into can manage its login")
    if owner is None and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to set up Scalable Capital")


def _error(exc: ScalableError) -> HTTPException:
    if isinstance(exc, (scalable.ScalableBusy, scalable.ScalableDisabled, scalable.ScalableOwnedByOtherUser)):
        status = 409
    elif isinstance(exc, ScalableLoginRequired):
        # 424, not 401: it is the server's sc session that expired, not the app login.
        status = 424
    else:
        status = 502
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message, "hints": exc.hints})


@router.get("/status", response_model=ScalableStatusOut)
def get_status(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    return scalable_status(db, user.id)


@router.post("/sync", response_model=ScalableSyncLogOut)
def post_sync(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    try:
        return scalable.sync(db, user.id, trigger="manual")
    except ScalableError as exc:
        raise _error(exc) from None


@router.post("/test", response_model=ScalableProbeOut)
def post_test(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    # The probe runs the server-wide sc session: only its owner (or an admin before the first sync).
    require_connection_owner(db, user)
    return scalable.probe(db)


@router.get("/sync/logs", response_model=list[ScalableSyncLogOut])
def get_sync_logs(limit: int = 20, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    return scalable.sync_logs(db, user.id, limit=limit)


@router.get("/positions", response_model=list[ScalablePositionOut])
def get_positions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[ScalablePositionOut]:
    return [
        ScalablePositionOut(
            id=p.id,
            isin=p.isin,
            ticker=p.ticker,
            name=p.name,
            security_type=p.security_type,
            quantity=float(p.quantity),
            pending_quantity=float(p.pending_quantity) if p.pending_quantity is not None else None,
            avg_buy_price=float(p.avg_buy_price) if p.avg_buy_price is not None else None,
            current_price=float(p.current_price) if p.current_price is not None else None,
            current_value=float(p.current_value) if p.current_value is not None else None,
            currency=p.currency,
            price_timestamp=p.price_timestamp,
            price_outdated=bool(p.price_outdated),
            last_synced=p.last_synced,
        )
        for p in broker_positions(db, user.id, source="scalable")
    ]


@router.get("/savings-plans", response_model=list[ScalableSavingsPlanOut])
def get_savings_plans(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    return scalable.savings_plans(db, user.id)


@router.get("/reconcile", response_model=list[ScalableReconcileRowOut])
def get_reconcile(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    """Dry run: hand-entered holdings that match a synced Scalable position. Writes nothing."""
    return scalable.reconciliation_preview(db, user.id)


@router.post("/reconcile", response_model=ScalableReconcileResultOut)
def post_reconcile(payload: ScalableReconcileIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, int]:
    """Apply the owner's confirmed decisions (``replace`` deletes the manual row)."""
    return scalable.apply_reconciliation(db, user.id, dict(payload.decisions))


@router.post("/login", response_model=ScalableLoginOut)
def post_login(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    """Start ``sc login --local-read-only`` and return the code to approve in the Scalable app."""
    require_connection_owner(db, user)
    try:
        return scalable_login.start(scalable.make_cli(db), user_id=user.id)
    except ScalableError as exc:
        raise _error(exc) from exc


@router.get("/login", response_model=ScalableLoginOut)
def get_login(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    require_connection_owner(db, user)
    return scalable_login.status()


@router.delete("/login", response_model=ScalableLoginOut)
def delete_login(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    require_connection_owner(db, user)
    return scalable_login.cancel()


@router.post("/pin-binary", response_model=ScalablePinOut)
def post_pin_binary(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    """Pin the SHA-256 of the installed ``sc`` (verified by the install script) so a swapped binary is refused."""
    require_connection_owner(db, user)
    digest = binary_sha256(SC_BINARY)
    if digest is None:
        raise HTTPException(status_code=404, detail={"code": "not_installed", "message": f"{SC_BINARY} not found"})
    upsert_public_settings(db, {"scalable_binary_sha256": digest})
    return {"sha256": digest, "pinned": True}
