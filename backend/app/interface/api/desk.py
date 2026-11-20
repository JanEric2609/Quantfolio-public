"""The guarded derivatives desk (ADR 0027): paper only, admin only. No order endpoint exists."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.decision.desk import (
    DeskActionError,
    chain_status,
    close_position_api,
    desk_status,
    list_events,
    list_news,
    list_positions,
    scorecard,
    set_halt,
    submit_idea,
    take_proposal,
)
from app.foundation.auth import require_admin
from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.interface.api.params import ROWS_BOUND

router = APIRouter(prefix="/api/desk", tags=["desk"])


class IdeaIn(BaseModel):
    text: str = Field(min_length=3, max_length=120, description="e.g. 'DAX up today ko'")


class TakeIn(BaseModel):
    nonce: str = Field(min_length=4, max_length=64)


class HaltIn(BaseModel):
    reason: str = Field(default="halted from the app", max_length=200)


def _raise(exc: DeskActionError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@router.get("/status")
def get_status(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return desk_status(db, user.id)


@router.get("/positions")
def get_positions(
    status: str | None = Query(default=None, pattern=r"^(proposed|expired|rejected|open|closed)$"),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return list_positions(db, user.id, status)


@router.get("/events")
def get_events(
    after_seq: Annotated[int, Query(), ROWS_BOUND] = 0, limit: int = Query(default=200, ge=1, le=500),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return list_events(db, after_seq, limit)


@router.get("/news")
def get_news(
    limit: int = Query(default=50, ge=1, le=200), sent_only: bool = False,
    user: User = Depends(require_admin), db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """The news wire (Alpaca stream, Fed, ECB, SEC 8-K) with the local model's rating. Alert-only."""
    return list_news(db, limit, sent_only)


@router.get("/scorecard")
def get_scorecard(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return scorecard(db)


@router.get("/chain")
def get_chain(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return chain_status(db)


@router.post("/ideas")
def post_idea(body: IdeaIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return submit_idea(db, user.id, body.text.strip())
    except DeskActionError as exc:
        raise _raise(exc) from exc


@router.post("/positions/{position_id}/take")
def post_take(position_id: str, body: TakeIn, user: User = Depends(require_admin),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return take_proposal(db, user.id, position_id, body.nonce)
    except DeskActionError as exc:
        raise _raise(exc) from exc


@router.post("/positions/{position_id}/close")
def post_close(position_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return close_position_api(db, user.id, position_id)
    except DeskActionError as exc:
        raise _raise(exc) from exc


@router.post("/halt")
def post_halt(body: HaltIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    set_halt(db, True, body.reason, f"app:{user.username}")
    return desk_status(db, user.id)


@router.post("/resume")
def post_resume(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    set_halt(db, False, "resumed from the app", f"app:{user.username}")
    return desk_status(db, user.id)
