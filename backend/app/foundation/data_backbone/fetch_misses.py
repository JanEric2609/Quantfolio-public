"""Symbols the providers could not serve, remembered across restarts (plan 2026-11-11).

A miss waits :data:`RETRY_SOON` before the next try; a symbol that failed on
:data:`MANY_DAYS` different days waits :data:`RETRY_LATE`. A success clears it.
Every function is best-effort: a failing write never fails the caller.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.foundation.models.entities import FetchMiss

logger = logging.getLogger(__name__)

RETRY_SOON = timedelta(hours=24)
RETRY_LATE = timedelta(days=7)
MANY_DAYS = 3


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def recently_missed(db: Any, symbol: str, *, now: datetime | None = None) -> tuple[bool, str | None]:
    """``(still inside the wait after its last miss, the miss's message)``."""
    now = now or datetime.now(UTC)
    try:
        row = db.get(FetchMiss, symbol.upper())
    except Exception:  # noqa: BLE001
        db.rollback()
        return False, None
    if row is None:
        return False, None
    wait = RETRY_LATE if row.misses >= MANY_DAYS else RETRY_SOON
    if now - _aware(row.last_miss) < wait:
        return True, row.message
    return False, None


def record_miss(db: Any, symbol: str, message: str | None = None, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    try:
        row = db.get(FetchMiss, symbol.upper())
        if row is None:
            db.add(FetchMiss(symbol=symbol.upper(), misses=1, last_miss=now, message=(message or "")[:200] or None))
        else:
            if _aware(row.last_miss).date() != now.date():
                row.misses += 1
            row.last_miss = now
            row.message = (message or "")[:200] or row.message
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.debug("fetch_misses: could not record %s", symbol, exc_info=True)


def clear_miss(db: Any, symbol: str) -> None:
    try:
        row = db.get(FetchMiss, symbol.upper())
        if row is not None:
            db.delete(row)
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
