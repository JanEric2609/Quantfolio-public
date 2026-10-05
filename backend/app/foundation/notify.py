"""Notification dispatcher for nudges and alerts.

Routes nudge notifications to: bell (in-app), email (SMTP), telegram.
Bell notifications persist as dedicated ``Notification`` rows
(re-homed from the old review inbox per audit-fixes-2026-08 todo 7); email
uses ``app.foundation.email`` for real SMTP delivery.
"""

import json
import logging
from typing import Literal

from sqlalchemy.orm import Session

from app.foundation.models.entities import Notification, Nudge, User

logger = logging.getLogger(__name__)


def create_notification(
    db: Session,
    user_id: str,
    source: str,
    title: str,
    body: str | None = None,
    severity: str = "info",
    href: str | None = None,
) -> Notification:
    """Persist an in-app bell notification and return the created row.

    Shared writer for the bell channel and later notification emitters
    (dkb diagnostics, verification, ...).
    """
    row = Notification(
        user_id=user_id,
        source=source,
        title=title,
        body=body,
        severity=severity,
        href=href,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def send_notification(
    db: Session,
    user_id: str,
    channel: Literal["bell", "email", "telegram"],
    nudge: Nudge,
) -> bool:
    """Route nudge to notification channel.

    Args:
        db: Database session
        user_id: User ID
        channel: Notification channel ("bell", "email", or "telegram")
        nudge: Nudge object to send

    Returns:
        True if sent successfully, False otherwise
    """
    try:
        if channel == "bell":
            return _send_bell(db, user_id, nudge)
        elif channel == "email":
            return _send_email(db, user_id, nudge)
        elif channel == "telegram":
            return _send_telegram(db, user_id, nudge)
        else:
            logger.warning(f"Unknown notification channel: {channel}")
            return False
    except Exception as e:
        logger.error(f"Failed to send {channel} notification: {e}")
        return False


def _send_bell(db: Session, user_id: str, nudge: Nudge) -> bool:
    """Send in-app bell notification as a dedicated Notification row.

    Appears in the NotificationCenter bell popover. Payload details remain
    available via the email/telegram channels; the bell keeps title+severity.
    """
    try:
        create_notification(
            db,
            user_id,
            source="nudge",
            title=_format_nudge_message(nudge),
            severity=nudge.severity,
        )
        logger.info(f"Bell notification sent to {user_id}: {nudge.id}")
        return True
    except Exception as e:
        logger.error(f"Bell notification failed: {e}")
        return False


def _send_email(db: Session, user_id: str, nudge: Nudge) -> bool:
    """Send email notification via SMTP.

    Resolves the user's email from the User model; returns False when
    the user record or email address is not available.
    """
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            logger.warning("Email notification skipped: user %s not found", user_id)
            return False
        recipient = getattr(user, "email", None)
        if not recipient:
            logger.warning("Email notification skipped: user %s has no email address", user_id)
            return False

        from app.foundation.email import send_email

        subject = f"Quantfolio: {nudge.source.replace('_', ' ').title()}"
        body = _format_nudge_email_body(nudge)

        return send_email(db, recipient, subject, body)
    except Exception as exc:
        logger.error("Email notification failed: %s", exc)
        return False


def _send_telegram(db: Session, user_id: str, nudge: Nudge) -> bool:
    """Send Telegram notification via the linked TelegramAccount, if any."""
    from app.foundation.telegram_bot import get_linked_account, send_telegram_message

    account = get_linked_account(db, user_id)
    if account is None:
        logger.info("Telegram notification skipped: user %s has no linked Telegram account", user_id)
        return False
    return send_telegram_message(db, account.chat_id, _format_nudge_message(nudge))


def _format_nudge_message(nudge: Nudge) -> str:
    """Format nudge as a short in-app message."""
    payload = json.loads(nudge.payload_json) if isinstance(nudge.payload_json, str) else nudge.payload_json
    source = nudge.source.replace("_", " ").title()

    if nudge.source == "high_conviction_recommendation":
        return f"High-conviction recommendation for {payload.get('symbol', '?')} (conviction: {payload.get('conviction', 0):.1%})"
    elif nudge.source == "regime_change":
        return f"Market regime changed to {payload.get('new_regime', '?')}"
    elif nudge.source == "drift_breach":
        return "Shadow portfolio diverged from real portfolio"
    elif nudge.source == "dkb_sync_error":
        return f"DKB sync failed: {payload.get('error', '?')}"
    elif nudge.source == "provider_health_drop":
        return f"Provider health degraded: {payload.get('provider', '?')}"
    elif nudge.source == "job_failure":
        return f"Scheduled job failed: {payload.get('job_name', '?')}"
    elif nudge.source == "budget_threshold":
        return f"{payload.get('category_name', 'A category')} envelope is at {payload.get('label', '?')} of its budget"
    else:
        return source


def _format_nudge_email_body(nudge: Nudge) -> str:
    """Format nudge as a detailed email body."""
    payload = json.loads(nudge.payload_json) if isinstance(nudge.payload_json, str) else nudge.payload_json

    header = f"Quantfolio Nudge: {nudge.source.replace('_', ' ').title()}\n"
    header += f"Severity: {nudge.severity.upper()}\n"
    header += f"Time: {nudge.ts.isoformat()}\n\n"

    if nudge.source == "high_conviction_recommendation":
        body = (
            f"High-conviction recommendation detected:\n"
            f"  Symbol: {payload.get('symbol', 'N/A')}\n"
            f"  Conviction: {payload.get('conviction', 0):.1%}\n"
            f"  Expected Sharpe: {payload.get('expected_sharpe', 0):.2f}\n\n"
            f"Review the recommendation in the AlphaCrafter Dossiers page."
        )
    elif nudge.source == "regime_change":
        body = (
            f"Market regime has changed:\n"
            f"  Previous: {payload.get('old_regime', 'N/A')}\n"
            f"  Current: {payload.get('new_regime', 'N/A')}\n\n"
            f"Your shadow portfolio may adjust its strategy."
        )
    elif nudge.source == "drift_breach":
        body = (
            f"Your shadow portfolio has diverged from your real portfolio:\n"
            f"  Anomaly type: {payload.get('anomaly_type', 'N/A')}\n"
            f"  Details: {payload.get('details', 'N/A')}\n\n"
            f"Check the Verification page for details."
        )
    elif nudge.source == "budget_threshold":
        body = (
            f"Your \"{payload.get('category_name', 'category')}\" envelope has reached "
            f"{payload.get('label', '?')} of its monthly budget.\n\n"
            f"Review your spending in the Money tab."
        )
    elif nudge.source == "job_failure":
        body = (
            f"Scheduled job \"{payload.get('job_name', 'N/A')}\" failed:\n"
            f"  {payload.get('error', 'N/A')}\n\n"
            f"Check the job run history for details."
        )
    else:
        body = f"Details:\n{json.dumps(payload, indent=2)}"

    return header + body
