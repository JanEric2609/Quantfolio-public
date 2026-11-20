"""SMTP email sender for nudge/alert notifications.

Reads configuration from public settings + the encrypted ``smtp`` secret
stored in the ``api_keys`` table. Supports STARTTLS (port 587) and SSL
(port 465) transports.
"""

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.settings import get_public_settings, get_secret

logger = logging.getLogger(__name__)


def get_smtp_config(db: Session) -> dict[str, Any] | None:
    """Read SMTP config from public settings + secret store.

    Returns a dict with keys ``host``, ``port``, ``use_tls``, ``from_address``,
    ``username``, ``password`` and ``allow_plaintext``. Returns ``None`` when ``smtp_host`` is
    empty (SMTP not configured).
    """
    settings = get_public_settings(db)
    host = settings.get("smtp_host", "").strip()
    if not host:
        return None

    secret_data = get_secret(db, "smtp")
    password = ""
    meta: dict[str, Any] = {}
    if secret_data:
        password, meta = secret_data

    return {
        "host": host,
        "port": int(settings.get("smtp_port", 587)),
        "use_tls": bool(settings.get("smtp_use_tls", True)),
        "from_address": settings.get(
            "smtp_from_address", "noreply@quantfolio.local"
        ).strip(),
        "username": meta.get("username", settings.get("smtp_username", "")).strip(),
        "password": password,
        "allow_plaintext": bool(settings.get("smtp_allow_plaintext", False)),
    }


def _redact_address(address: str) -> str:
    """Redact email address for logging (keep domain, mask local part)."""
    if "@" not in address:
        return "***"
    local, domain = address.rsplit("@", 1)
    if len(local) <= 2:
        visible = local[:1]
    else:
        visible = local[:2]
    return f"{visible}***@{domain}"


def send_email(
    db: Session,
    to_address: str,
    subject: str,
    body: str,
    *,
    config_override: dict[str, Any] | None = None,
) -> bool:
    """Send a plain-text email via SMTP.

    Args:
        db: Database session used to load SMTP configuration.
        to_address: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        config_override: Optional dict that overrides the SMTP config loaded
            from settings/secret store. Useful for testing.

    Returns:
        ``True`` if the message was accepted by the SMTP server, ``False``
        otherwise (errors are logged).
    """
    try:
        config = config_override or get_smtp_config(db)
        if config is None:
            logger.warning("SMTP not configured; cannot send email")
            return False

        msg = EmailMessage()
        msg.set_content(body)
        msg["Subject"] = subject
        msg["From"] = config["from_address"]
        msg["To"] = to_address

        host: str = config["host"]
        port: int = config["port"]
        username: str = config["username"]
        password: str = config["password"]
        use_tls: bool = config["use_tls"]

        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        elif use_tls:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls()
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        else:
            # Neither SSL (465) nor STARTTLS: credentials and mail cross the network
            # in clear. Only with an explicit opt-in (a trusted local relay).
            if not config.get("allow_plaintext"):
                logger.error(
                    "Refusing to send email over unencrypted SMTP (%s:%s). Enable STARTTLS or SSL, "
                    "or set smtp_allow_plaintext for a trusted local relay.",
                    host,
                    port,
                )
                return False
            with smtplib.SMTP(host, port, timeout=15) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(msg)

        logger.info("Email sent to <%s>: %s", _redact_address(to_address), subject)
        return True
    except Exception as exc:
        logger.error("Failed to send email to <%s>: %s", _redact_address(to_address), exc)
        return False


def send_test_email(db: Session, to_address: str) -> tuple[bool, str]:
    """Send a test email to verify the SMTP configuration.

    Returns:
        Tuple of ``(success, message)`` for display in the Control Center.
    """
    config = get_smtp_config(db)
    if config is None:
        return False, "SMTP is not configured (smtp_host is empty)"
    if config["port"] != 465 and not config["use_tls"] and not config.get("allow_plaintext"):
        return False, (
            "SMTP encryption is off. Turn on STARTTLS (or use port 465), "
            "or allow unencrypted SMTP for a trusted local relay."
        )

    success = send_email(
        db,
        to_address,
        subject="Quantfolio - Test Email",
        body=(
            "This is a test email from Quantfolio. If you received this, "
            "your SMTP configuration is working correctly."
        ),
        config_override=config,
    )
    if success:
        return True, f"Test email sent to {to_address}"
    return False, "Failed to send test email; check SMTP configuration and server logs"
