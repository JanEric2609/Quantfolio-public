"""Dedicated notifications API (plan todo 7).

Read/mark-read surface for the in-app bell. Writers use
``app.foundation.notify.create_notification`` — there is intentionally no
POST-create endpoint (notifications are system-emitted only).
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import Notification, User
from app.foundation.auth import current_user

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_MAX_LIMIT = 100


def _to_dict(row: Notification) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": row.source,
        "title": row.title,
        "body": row.body,
        "severity": row.severity,
        "href": row.href,
        "read_at": row.read_at.isoformat() if row.read_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/")
def list_notifications(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Latest own notifications (newest first) plus the unread count."""
    limit = max(1, min(limit, _MAX_LIMIT))
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .all()
    )
    unread_count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read_at.is_(None))
        .count()
    )
    return {"items": [_to_dict(row) for row in rows], "unread_count": unread_count}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, int]:
    now = datetime.now(UTC)
    updated = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read_at.is_(None))
        .update({"read_at": now}, synchronize_session=False)
    )
    db.commit()
    return {"updated": updated}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(row)
    return _to_dict(row)
