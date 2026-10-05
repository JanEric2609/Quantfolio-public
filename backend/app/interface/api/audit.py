"""Audit log API endpoints."""


from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.foundation.models.entities import AuditLog, User
from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation import audit as audit_service

router = APIRouter(prefix="/api/audit", tags=["audit"])


class AuditEventResponse(BaseModel):
    id: str
    user_id: str
    event_type: str
    source: str
    severity: str
    message: str
    payload_json: str
    correlation_id: str | None
    created_at: str

    model_config = ConfigDict(from_attributes=True)


@router.get("", response_model=list[AuditEventResponse])
def list_audit_events(
    event_type: str | None = None,
    source: str | None = None,
    severity: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[AuditLog]:
    """List audit events with optional filters."""
    return audit_service.read_audit_events(
        db,
        user_id=user.id,
        event_type=event_type,
        source=source,
        severity=severity,
        limit=limit,
    )


@router.get("/{event_id}", response_model=AuditEventResponse)
def get_audit_event(
    event_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> AuditLog:
    """Get a specific audit event."""
    event = audit_service.get_audit_event(db, event_id)
    if not event or event.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event


@router.get("/correlation/{correlation_id}", response_model=list[AuditEventResponse])
def get_events_by_correlation(
    correlation_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[AuditLog]:
    """Get all events in a correlation group."""
    events = audit_service.get_audit_events_by_correlation_id(db, correlation_id)
    # Filter by user
    user_events = [e for e in events if e.user_id == user.id]
    if not user_events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Correlation group not found")
    return user_events
