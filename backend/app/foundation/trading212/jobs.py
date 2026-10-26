"""Scheduled Trading 212 sync (worker)."""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_interval_job

logger = logging.getLogger(__name__)

# The job wakes hourly and syncs once ``trading212_sync_hours`` have passed
# since the last successful sync, so the interval is a live setting.
CHECK_EVERY_HOURS = 1


def trading212_sync_once(db: Any | None = None) -> dict[str, Any]:
    """Sync the owner's account while the connection is enabled and a key is saved.

    Only the user who ran the first (manual) sync is synced: the key is
    installation-wide. A rejected key ends as a warning in the sync log and is
    not retried faster than the sync interval.
    """
    from datetime import UTC, datetime, timedelta

    from app.foundation.core.db import SessionLocal
    from app.foundation.settings import get_public_settings

    from .client import Trading212Error
    from .service import Trading212Busy, sync
    from app.foundation.trading212_status import owner_user_id, status, sync_hours

    own = db is None
    session = db or SessionLocal()
    try:
        settings = get_public_settings(session)
        if not settings.get("trading212_enabled"):
            return {"status": "skipped", "reason": "disabled"}
        user_id = owner_user_id(session)
        if user_id is None:
            return {"status": "skipped", "reason": "no_owner"}
        every = timedelta(hours=sync_hours(settings))
        now = datetime.now(UTC)
        current = status(session, user_id)
        if current["state"] == "not_configured":
            return {"status": "skipped", "reason": "not_configured"}
        last = current.get("last_sync_at")
        if current["state"] in {"key_invalid", "key_scope"} and last and now - last < every:
            return {"status": "skipped", "reason": current["state"]}
        last_ok = current.get("last_success_at")
        if last_ok and now - last_ok < every - timedelta(minutes=5):
            return {"status": "skipped", "reason": "not_due"}
        try:
            log = sync(session, user_id, trigger="scheduled")
        except Trading212Busy:
            return {"status": "skipped", "reason": "busy"}
        except Trading212Error as exc:
            return {"status": "warning", "reason": exc.code}
        return {"status": "ok", "state": log["state"]}
    finally:
        if own:
            session.close()


def register_trading212_sync_job(scheduler: Any | None = None) -> str:
    return register_interval_job(
        "trading212_sync",
        trading212_sync_once,
        scheduler=scheduler,
        hours=CHECK_EVERY_HOURS,
    )
