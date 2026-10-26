"""Trading 212 — read-only sync through the public REST API.

No endpoint here can trade or change anything at Trading 212: the client only
sends allow-listed GET requests (see ``foundation/trading212/client.py``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.live_positions import broker_positions
from app.foundation.models.entities import User
from app.foundation.trading212 import service as t212
from app.foundation.trading212.client import Trading212AuthError, Trading212Error, Trading212ScopeError
from app.foundation.trading212_status import owner_user_id
from app.foundation.trading212_status import status as t212_status
from app.interface.api.params import LimitQuery

router = APIRouter(prefix="/api/trading212", tags=["trading212"])


class Trading212StatusOut(BaseModel):
    source: str
    enabled: bool
    state: str
    environment: str = "live"
    message: str = ""
    error_code: str | None = None
    last_sync_at: datetime | None = None
    last_success_at: datetime | None = None
    stale: bool = False
    account_id: str | None = None
    tracking_since: str | None = None
    positions: int = 0
    pies: int = 0
    pies_synced_at: str | None = None
    pies_unavailable: bool = False
    pies_stale: bool = False
    interest_30d_eur: str | None = None
    interest_rate_measured: str | None = None
    pending_reconciliation: int = 0
    read_only: bool = True


class Trading212SyncLogOut(BaseModel):
    id: str
    source: str
    trigger: str
    state: str
    error_code: str | None = None
    message: str = ""
    counts: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Trading212PositionOut(BaseModel):
    id: str
    isin: str
    ticker: str | None = None
    name: str
    security_type: str | None = None
    quantity: float
    avg_buy_price: float | None = None
    current_price: float | None = None
    current_value: float | None = None
    currency: str
    last_synced: datetime | None = None


class Trading212PieInstrumentOut(BaseModel):
    t212_ticker: str | None = None
    isin: str | None = None
    name: str | None = None
    expected_share: str | None = None
    current_share: str | None = None
    owned_quantity: str | None = None
    value: str | None = None
    result: str | None = None
    issues: list[dict[str, str]] = Field(default_factory=list)


class Trading212PieOut(BaseModel):
    id: str
    name: str
    status: str | None = None
    cash: float | None = None
    invested: float | None = None
    value: float | None = None
    result: float | None = None
    result_pct: float | None = None
    dividends_gained: float | None = None
    dividends_reinvested: float | None = None
    dividends_in_cash: float | None = None
    dividend_action: str | None = None
    goal: float | None = None
    end_date: str | None = None
    instruments: list[Trading212PieInstrumentOut] = Field(default_factory=list)
    synced_at: datetime | None = None


class Trading212ProbeStepOut(BaseModel):
    name: str
    ok: bool
    detail: str


class Trading212ProbeOut(BaseModel):
    ok: bool
    message: str
    steps: list[Trading212ProbeStepOut] = Field(default_factory=list)
    code: str | None = None


def require_connection_owner(db: Session, user: User) -> None:
    """The saved key is installation-wide: only the user it syncs into (or,
    before the first sync, an admin) may test it."""
    owner = owner_user_id(db)
    if owner is not None and owner != user.id:
        raise HTTPException(status_code=403, detail="Only the user Trading 212 syncs into can manage its key")
    if owner is None and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to set up Trading 212")


def _error(exc: Trading212Error) -> HTTPException:
    if isinstance(exc, (t212.Trading212Busy, t212.Trading212Disabled, t212.Trading212OwnedByOtherUser,
                        t212.Trading212NotConfigured, t212.Trading212AccountChanged)):
        status = 409
    elif isinstance(exc, (Trading212AuthError, Trading212ScopeError)):
        # 424, not 401: it is the Trading 212 key that was refused, not the app login.
        status = 424
    else:
        status = 502
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


@router.get("/status", response_model=Trading212StatusOut)
def get_status(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    return t212_status(db, user.id)


@router.post("/sync", response_model=Trading212SyncLogOut)
def post_sync(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    try:
        return t212.sync(db, user.id, trigger="manual")
    except Trading212Error as exc:
        raise _error(exc) from None


@router.post("/test", response_model=Trading212ProbeOut)
def post_test(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    require_connection_owner(db, user)
    return t212.probe(db)


@router.get("/sync/logs", response_model=list[Trading212SyncLogOut])
def get_sync_logs(limit: LimitQuery = 20, db: Session = Depends(get_db),
                  user: User = Depends(current_user)) -> list[dict[str, Any]]:
    return t212.sync_logs(db, user.id, limit=limit)


@router.get("/positions", response_model=list[Trading212PositionOut])
def get_positions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[Trading212PositionOut]:
    return [
        Trading212PositionOut(
            id=p.id, isin=p.isin, ticker=p.ticker, name=p.name, security_type=p.security_type,
            quantity=float(p.quantity), avg_buy_price=_num(p.avg_buy_price), current_price=_num(p.current_price),
            current_value=_num(p.current_value), currency=p.currency, last_synced=p.last_synced,
        )
        for p in broker_positions(db, user.id, source="trading212")
    ]


@router.get("/pies", response_model=list[Trading212PieOut])
def get_pies(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, Any]]:
    """The pies as last read (the read fails soft; ``synced_at`` says how fresh)."""
    out = []
    for pie in t212.pies(db, user.id):
        out.append({k: (_num(v) if k in {
            "cash", "invested", "value", "result", "result_pct", "dividends_gained", "dividends_reinvested",
            "dividends_in_cash", "goal"} else v) for k, v in pie.items()})
    return out
