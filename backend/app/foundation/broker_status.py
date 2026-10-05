"""Scalable Capital connection status, without running ``sc``.

A flat foundation module (not part of the ``scalable`` package) so the Control
Center can read it without importing the CLI layer: ``scalable`` sits above
the flat foundation modules, never beside them in an import cycle.
"""
from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any, overload

from sqlalchemy.orm import Session

from app.foundation.models.entities import BrokerPosition, BrokerSyncLog, ConnectedAccount
from app.foundation.settings import get_public_settings

SOURCE = "scalable"
RUNNING_TIMEOUT = timedelta(minutes=20)
STALE_AFTER = timedelta(days=3)
LOGIN_CODES = frozenset({"no_session", "refresh_relogin_required", "secret_storage_unavailable"})


def depot_account(db: Session, user_id: str) -> ConnectedAccount | None:
    return (
        db.query(ConnectedAccount)
        .filter(
            ConnectedAccount.user_id == user_id,
            ConnectedAccount.source == SOURCE,
            ConnectedAccount.account_type == "depot",
        )
        .order_by(ConnectedAccount.created_at)
        .first()
    )


def load_raw(account: ConnectedAccount | None) -> dict[str, Any]:
    if account is None:
        return {}
    try:
        raw = json.loads(account.raw_json or "{}")
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def number_setting(settings: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    """A numeric Control Center setting clamped to ``[low, high]``; unparseable means the default."""
    try:
        value = float(settings.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, low), high)


@overload
def aware_utc(value: datetime) -> datetime: ...
@overload
def aware_utc(value: datetime | None) -> datetime | None: ...
def aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def owner_user_id(db: Session) -> str | None:
    """The app user the server's single sc session syncs into, once one has synced.

    There is one sc login per server, so its portfolio belongs to exactly one
    app user: whoever ran the first successful sync. Another user can never
    sync it into their own book.
    """
    row = (
        db.query(ConnectedAccount.user_id)
        .filter(ConnectedAccount.source == SOURCE, ConnectedAccount.account_type == "depot")
        .order_by(ConnectedAccount.created_at)
        .first()
    )
    return row[0] if row else None


def status(db: Session, user_id: str) -> dict[str, Any]:
    """Cheap status for the UI and attention items. Never runs sc."""
    from app.foundation.live_positions import pending_reconciliation

    settings = get_public_settings(db)
    last = (
        db.query(BrokerSyncLog)
        .filter(BrokerSyncLog.user_id == user_id, BrokerSyncLog.source == SOURCE)
        .order_by(BrokerSyncLog.started_at.desc())
        .first()
    )
    last_success = (
        db.query(BrokerSyncLog)
        .filter(
            BrokerSyncLog.user_id == user_id,
            BrokerSyncLog.source == SOURCE,
            BrokerSyncLog.state.in_(("success", "warning")),
            BrokerSyncLog.error_code.is_(None),
        )
        .order_by(BrokerSyncLog.started_at.desc())
        .first()
    )
    depot = depot_account(db, user_id)
    raw = load_raw(depot)
    positions = db.query(BrokerPosition).filter(
        BrokerPosition.user_id == user_id, BrokerPosition.source == SOURCE
    ).count()
    enabled = bool(settings.get("scalable_enabled"))
    now = datetime.now(UTC)

    if not enabled:
        state = "disabled"
    elif last is not None and last.state == "running" and now - (aware_utc(last.started_at) or now) < RUNNING_TIMEOUT:
        state = "running"
    elif last is None:
        state = "never_synced" if os.path.exists(str(settings.get("scalable_wrapper_path") or "")) else "not_installed"
    elif last.error_code in LOGIN_CODES:
        state = "login_required"
    elif last.error_code == "guard_not_attested":
        state = "guard_unattested"
    elif last.error_code in {"not_installed", "sudo_not_configured", "wrapper_untrusted"}:
        state = "not_installed"
    elif last.state == "error":
        state = "error"
    else:
        state = "ready"

    last_success_at = aware_utc(last_success.finished_at or last_success.started_at) if last_success else None
    return {
        "source": SOURCE,
        "enabled": enabled,
        "state": state,
        "message": (last.message if last else ""),
        "error_code": last.error_code if last else None,
        "last_sync_at": aware_utc(last.started_at) if last else None,
        "last_success_at": last_success_at,
        "stale": bool(enabled and last_success_at and now - last_success_at > STALE_AFTER),
        "portfolio_id": raw.get("portfolio_id") or settings.get("scalable_portfolio_id") or None,
        "tracking_since": raw.get("tracking_since"),
        "positions": positions,
        "pending_reconciliation": len(pending_reconciliation(db, user_id)),
        "read_only": True,
    }
