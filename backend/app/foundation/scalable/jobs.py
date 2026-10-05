"""Scheduled Scalable sync (worker). No TAN is needed, unlike DKB."""
from __future__ import annotations

import logging
from typing import Any

from app.foundation.jobs import register_interval_job

logger = logging.getLogger(__name__)

# The job wakes hourly and syncs once ``scalable_sync_hours`` have passed since
# the last successful sync, so the interval is a live setting without a
# worker restart.
CHECK_EVERY_HOURS = 1


def scalable_sync_once(db: Any | None = None) -> dict[str, Any]:
    """Sync the owner's portfolio while the connection is enabled.

    Only the user who ran the first (manual) sync is synced: the server has
    one sc login, so there is no portfolio to fetch for anyone else. Login
    problems end as a warning in the sync log, never as a crash.
    """
    from datetime import UTC, datetime, timedelta

    from app.foundation.core.db import SessionLocal
    from app.foundation.settings import get_public_settings

    from .cli import ScalableError
    from app.foundation.broker_status import number_setting, owner_user_id, status

    from .service import ScalableBusy, sync

    own = db is None
    session = db or SessionLocal()
    try:
        settings = get_public_settings(session)
        if not settings.get("scalable_enabled"):
            return {"status": "skipped", "reason": "disabled"}
        user_id = owner_user_id(session)
        if user_id is None:
            return {"status": "skipped", "reason": "no_owner"}
        every = timedelta(hours=number_setting(settings, "scalable_sync_hours", 6, 1, 48))
        now = datetime.now(UTC)
        current = status(session, user_id)
        last = current.get("last_sync_at")
        if current["state"] in {"login_required", "not_installed", "guard_unattested"} and last and now - last < every:
            # A problem only a human can fix: don't retry it faster than the sync interval.
            return {"status": "skipped", "reason": current["state"]}
        last_ok = current.get("last_success_at")
        if last_ok and now - last_ok < every - timedelta(minutes=5):
            return {"status": "skipped", "reason": "not_due"}
        try:
            log = sync(session, user_id, trigger="scheduled")
        except ScalableBusy:
            return {"status": "skipped", "reason": "busy"}
        except ScalableError as exc:
            return {"status": "warning", "reason": exc.code}
        return {"status": "ok", "state": log["state"]}
    finally:
        if own:
            session.close()


def register_scalable_sync_job(scheduler: Any | None = None) -> str:
    return register_interval_job(
        "scalable_sync",
        scalable_sync_once,
        scheduler=scheduler,
        hours=CHECK_EVERY_HOURS,
    )
