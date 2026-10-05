"""Audit event logging service for Phase 5 verification loop.

Records all verification events: recommendations, regime changes, drift anomalies,
Q-learning updates, MAML steps, etc. Follows the llm_audit.py pattern.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.foundation.models.entities import AuditLog


def write_audit_event(
    db: Session,
    user_id: str,
    event_type: str,
    source: str,
    severity: str = "info",
    message: str = "",
    payload_json: dict[str, Any] | None = None,
    correlation_id: UUID | None = None,
) -> AuditLog:
    """Write an audit event to audit_logs table.

    Args:
        db: Database session
        user_id: User ID
        event_type: Type of event (e.g., "daily_reconciliation", "drift_breach", "recommendation_accepted")
        source: Source of event (e.g., "verification", "alphacrafter", "dkb")
        severity: "info", "warning", or "critical"
        message: Human-readable message
        payload_json: Optional structured payload (converted to JSON)
        correlation_id: Optional UUID to group related events

    Returns:
        The created AuditLog row
    """
    event = AuditLog(
        user_id=user_id,
        event_type=event_type,
        source=source,
        severity=severity,
        message=message,
        payload_json=str(payload_json or {}),
        correlation_id=str(correlation_id or uuid4()),
    )
    db.add(event)
    db.commit()
    return event


def read_audit_events(
    db: Session,
    user_id: str,
    event_type: str | None = None,
    source: str | None = None,
    severity: str | None = None,
    correlation_id: UUID | None = None,
    limit: int = 100,
) -> list[AuditLog]:
    """Read audit events with optional filters.

    Args:
        db: Database session
        user_id: User ID
        event_type: Filter by event type (optional)
        source: Filter by source (optional)
        severity: Filter by severity (optional)
        correlation_id: Filter by correlation ID (optional)
        limit: Maximum number of events to return

    Returns:
        List of AuditLog rows ordered by created_at DESC
    """
    q = db.query(AuditLog).filter(AuditLog.user_id == user_id)

    if event_type:
        q = q.filter(AuditLog.event_type == event_type)
    if source:
        q = q.filter(AuditLog.source == source)
    if severity:
        q = q.filter(AuditLog.severity == severity)
    if correlation_id:
        q = q.filter(AuditLog.correlation_id == correlation_id)

    return q.order_by(AuditLog.created_at.desc()).limit(limit).all()


def read_audit_events_by_type(
    db: Session,
    user_id: str,
    event_type: str,
    limit: int = 100,
) -> list[AuditLog]:
    """Convenience wrapper around `read_audit_events` for type-only filtering."""
    return read_audit_events(db, user_id=user_id, event_type=event_type, limit=limit)


def get_audit_event(db: Session, event_id: str) -> AuditLog | None:
    """Fetch a single audit event by its primary key, or None if missing."""
    return db.query(AuditLog).filter(AuditLog.id == event_id).first()


def get_audit_events_by_correlation_id(
    db: Session,
    correlation_id: str | UUID,
    limit: int = 100,
) -> list[AuditLog]:
    """Fetch every audit event sharing a correlation ID, newest first."""
    return (
        db.query(AuditLog)
        .filter(AuditLog.correlation_id == str(correlation_id))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
