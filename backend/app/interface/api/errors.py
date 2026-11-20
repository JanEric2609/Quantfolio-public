"""Unexpected errors and the daily self-check (reliability plan 2026-10-14).

The browser reports its own crashes here (React error boundary, unhandled
promise rejections); the API records its unhandled exceptions directly
(``main.unexpected_error``). Both are read back by the Control Center's
Status page.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.app_errors import MAX_DETAIL, MAX_LOCATION, MAX_MESSAGE, recent_errors, record_error
from app.foundation.auth import current_user, require_admin
from app.foundation.core.db import get_db
from app.foundation.core.security import limiter
from app.foundation.models.entities import User

router = APIRouter(prefix="/api/errors", tags=["errors"])


class ClientErrorIn(BaseModel):
    kind: str = Field(default="Error", max_length=120)
    message: str = Field(default="", max_length=MAX_MESSAGE)
    # The page's path, e.g. /portfolio/holdings (no query string: it can hold data).
    location: str = Field(default="", max_length=MAX_LOCATION)
    stack: str | None = Field(default=None, max_length=MAX_DETAIL)


class RecordedOut(BaseModel):
    recorded: bool


class AppErrorOut(BaseModel):
    id: str
    source: str
    kind: str
    message: str
    location: str
    count: int
    first_seen: datetime
    last_seen: datetime


class SelfCheckOut(BaseModel):
    ran_at: str | None = None
    problems: list[dict[str, Any]] = []
    notified: bool = False


@router.post("/client", response_model=RecordedOut)
@limiter.limit("30/minute")
def report_client_error(
    request: Request,
    payload: ClientErrorIn,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> RecordedOut:
    """Record a crash the browser caught. Signed-in users only, rate-limited."""
    location = payload.location.split("?", 1)[0].split("#", 1)[0]
    record_error(
        db, source="client", kind=payload.kind, message=payload.message,
        location=location, detail=payload.stack,
    )
    return RecordedOut(recorded=True)


@router.get("", response_model=list[AppErrorOut])
def list_errors(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[AppErrorOut]:
    """API and browser errors seen in the last *days*, most recent first."""
    return [AppErrorOut.model_validate(row) for row in recent_errors(db, since=timedelta(days=days))]


@router.get("/self-check", response_model=SelfCheckOut)
def last_self_check(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> SelfCheckOut:
    """The stored result of the last daily self-check (it runs from cron, not from here)."""
    from app.foundation.self_check import last_result

    result = last_result(db)
    return SelfCheckOut(**result) if result else SelfCheckOut()
