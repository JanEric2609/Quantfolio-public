"""Record unexpected errors where the owner will see them (reliability plan 2026-10-14).

Until now an unhandled API error was a log line in journald and a browser
error was nothing at all; neither reached the person using the app. Both now
land in ``app_errors``, grouped by a fingerprint so a repeat bumps a counter,
and the Control Center's attention list and the daily self-check report the
ones seen in the last day.

Self-hosted on purpose: tracebacks can carry portfolio data, and the stack
runs on the owner's LAN.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

Source = Literal["api", "client"]

MAX_MESSAGE = 1_000
MAX_LOCATION = 300
MAX_DETAIL = 8_000
# Rows not seen for this long are deleted by the self-check.
RETENTION = timedelta(days=90)

_VOLATILE = re.compile(r"0x[0-9a-f]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{27,}|\d+", re.IGNORECASE)


def fingerprint(source: str, kind: str, location: str, message: str) -> str:
    """Same error, same row: numbers, ids and addresses are masked before hashing."""
    stable = "|".join((source, kind, _VOLATILE.sub("#", location), _VOLATILE.sub("#", message)[:200]))
    return hashlib.sha256(stable.encode("utf-8", "replace")).hexdigest()


def record_error(
    db: Session,
    *,
    source: Source,
    kind: str,
    message: str,
    location: str,
    detail: str | None = None,
    now: datetime | None = None,
) -> None:
    """Insert or bump the error's row. Never raises: error reporting must not cause errors."""
    from app.foundation.models.entities import AppError

    when = now or datetime.now(UTC)
    kind = (kind or "Error")[:120]
    message = (message or "")[:MAX_MESSAGE]
    location = (location or "")[:MAX_LOCATION]
    key = fingerprint(source, kind, location, message)
    try:
        row = db.query(AppError).filter(AppError.fingerprint == key).one_or_none()
        if row is None:
            db.add(AppError(
                fingerprint=key, source=source, kind=kind, message=message, location=location,
                detail=(detail or None) and detail[-MAX_DETAIL:], count=1, first_seen=when, last_seen=when,
            ))
        else:
            row.count = (row.count or 0) + 1
            row.last_seen = when
            if detail:
                row.detail = detail[-MAX_DETAIL:]
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Could not record %s error %s", source, kind, exc_info=True)


def record_api_exception(exc: BaseException, location: str) -> None:
    """The API catch-all's hook: its own session, because the request's may be broken."""
    import traceback

    from app.foundation.core.db import SessionLocal

    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        with SessionLocal() as db:
            record_error(db, source="api", kind=type(exc).__name__, message=str(exc), location=location, detail=detail)
    except Exception:
        logger.warning("Could not open a session to record an API error", exc_info=True)


def recent_errors(db: Session, *, since: timedelta = timedelta(days=1), now: datetime | None = None) -> list[dict[str, object]]:
    """Errors seen within *since*, most recent first."""
    from app.foundation.models.entities import AppError

    cutoff = (now or datetime.now(UTC)) - since
    rows = db.query(AppError).filter(AppError.last_seen >= cutoff).order_by(AppError.last_seen.desc()).all()
    return [
        {
            "id": row.id,
            "source": row.source,
            "kind": row.kind,
            "message": row.message,
            "location": row.location,
            "count": row.count,
            "first_seen": row.first_seen,
            "last_seen": row.last_seen,
        }
        for row in rows
    ]


def prune_errors(db: Session, *, now: datetime | None = None) -> int:
    """Delete rows not seen within RETENTION; returns how many."""
    from app.foundation.models.entities import AppError

    cutoff = (now or datetime.now(UTC)) - RETENTION
    deleted = db.query(AppError).filter(AppError.last_seen < cutoff).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)
