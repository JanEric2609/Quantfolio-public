"""Trading 212 connection status without a request (Control Center, UI, the job).

A flat foundation module (not part of the ``trading212`` package) so the
Control Center reads it without importing the client: ``trading212`` sits
above the flat foundation modules, never beside them in an import cycle
(the same split as ``broker_status`` for Scalable).
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import ApiKey, BrokerPie, BrokerPosition, BrokerSyncLog, ConnectedAccount
from app.foundation.settings import get_public_settings

SOURCE = "trading212"
RUNNING_TIMEOUT = timedelta(minutes=30)
PIES_STALE_AFTER = timedelta(days=7)
KEY_CODES = frozenset({"unauthorized"})
SCOPE_CODES = frozenset({"forbidden"})


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def load_raw(account: ConnectedAccount | None) -> dict[str, Any]:
    if account is None:
        return {}
    try:
        raw = json.loads(account.raw_json or "{}")
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def sync_hours(settings: dict[str, Any]) -> float:
    try:
        value = float(settings.get("trading212_sync_hours"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 6.0
    return min(max(value, 1.0), 48.0) if math.isfinite(value) else 6.0


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


def owner_user_id(db: Session) -> str | None:
    """The app user the stored API key syncs into: whoever ran the first sync.

    The key is installation-wide (one ``api_keys`` row), so its account
    belongs to exactly one app user.
    """
    row = (
        db.query(ConnectedAccount.user_id)
        .filter(ConnectedAccount.source == SOURCE, ConnectedAccount.account_type == "depot")
        .order_by(ConnectedAccount.created_at)
        .first()
    )
    return row[0] if row else None


def configured(db: Session) -> bool:
    return db.query(ApiKey.id).filter(ApiKey.service == SOURCE).first() is not None


def status(db: Session, user_id: str) -> dict[str, Any]:
    """Cheap status for the UI and attention items. Never calls Trading 212."""
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
    raw = load_raw(depot_account(db, user_id))
    positions = db.query(BrokerPosition).filter(
        BrokerPosition.user_id == user_id, BrokerPosition.source == SOURCE
    ).count()
    pies = db.query(BrokerPie).filter(BrokerPie.user_id == user_id, BrokerPie.source == SOURCE).count()
    enabled = bool(settings.get("trading212_enabled"))
    now = datetime.now(UTC)

    if not enabled:
        state = "disabled"
    elif not configured(db):
        state = "not_configured"
    elif last is not None and last.state == "running" and now - (aware(last.started_at) or now) < RUNNING_TIMEOUT:
        state = "running"
    elif last is None:
        state = "never_synced"
    elif last.error_code in KEY_CODES:
        state = "key_invalid"
    elif last.error_code in SCOPE_CODES:
        state = "key_scope"
    elif last.state == "error":
        state = "error"
    else:
        state = "ready"

    last_success_at = aware(last_success.finished_at or last_success.started_at) if last_success else None
    stale_after = timedelta(hours=2 * sync_hours(settings))
    pies_ok_at = raw.get("pies_synced_at")
    pies_failing_since = raw.get("pies_failing_since")
    pies_stale = False
    if pies_failing_since:
        try:
            pies_stale = now - datetime.fromisoformat(str(pies_failing_since)) > PIES_STALE_AFTER
        except ValueError:
            pies_stale = False
    return {
        "source": SOURCE,
        "enabled": enabled,
        "state": state,
        "environment": str(settings.get("trading212_environment") or "live"),
        "message": (last.message if last else ""),
        "error_code": last.error_code if last else None,
        "last_sync_at": aware(last.started_at) if last else None,
        "last_success_at": last_success_at,
        "stale": bool(enabled and last_success_at and now - last_success_at > stale_after),
        "account_id": raw.get("account_id"),
        "tracking_since": raw.get("tracking_since"),
        "positions": positions,
        "pies": pies,
        "pies_synced_at": pies_ok_at,
        "pies_unavailable": bool(pies_failing_since),
        "pies_stale": pies_stale,
        "interest_30d_eur": raw.get("interest_30d_eur"),
        "interest_rate_measured": raw.get("interest_rate_measured"),
        "pending_reconciliation": len([
            p for p in pending_reconciliation(db, user_id) if p.source == SOURCE
        ]),
        "read_only": True,
    }
