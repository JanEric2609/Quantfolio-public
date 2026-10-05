"""Telegram bot integration: pairing, outbound delivery, freeform entry parsing.

Money Phase 3 (docs/archive/plans/student-budget-redesign.md §1.6, §1.7).
"""

import logging
import re
import secrets
import string
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.foundation.models.entities import Expense, TelegramAccount, TelegramPairingCode
from app.foundation.expense_rules import categorize_description
from app.foundation.settings import get_secret, update_secret_meta

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


def _silence_http_loggers() -> None:
    """Keep httpx/httpcore from logging request URLs (the bot token is in the path).

    main.py does this for the API process; the worker also polls Telegram, and
    this way the guarantee does not depend on how a given process configured
    logging. Idempotent and only ever raises a level.
    """
    for name in ("httpx", "httpcore"):
        http_logger = logging.getLogger(name)
        if http_logger.getEffectiveLevel() < logging.WARNING:
            http_logger.setLevel(logging.WARNING)


def get_bot_token(db: Session) -> str | None:
    secret = get_secret(db, "telegram")
    return secret[0] if secret else None


def send_telegram_message(db: Session, chat_id: str, text: str) -> bool:
    token = get_bot_token(db)
    if not token:
        logger.warning("Telegram message not sent: no bot token configured")
        return False
    _silence_http_loggers()
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            response.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        # Do not log str(exc)/the exception object: httpx.HTTPStatusError's
        # default message embeds the full request URL, which contains the
        # bot token (Telegram authenticates via URL path, not headers).
        logger.error(
            "Telegram sendMessage failed: HTTP %s from Telegram API",
            exc.response.status_code,
        )
        return False
    except Exception as exc:
        logger.error("Telegram sendMessage failed: %s", type(exc).__name__)
        return False


def _call_api(token: str, method: str, payload: dict[str, Any] | None = None, *, timeout: float = 10.0) -> Any | None:
    """POST one Bot API method; the decoded ``result`` on success, ``None`` on any failure.

    The bot token is part of the request URL, so nothing here may log the URL,
    the request, or an exception object (``httpx`` embeds the URL in both): only
    the method name and a status code / exception class go to the log.
    """
    _silence_http_loggers()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{TELEGRAM_API_BASE}/bot{token}/{method}", json=payload or {})
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Telegram %s failed: HTTP %s from Telegram API", method, exc.response.status_code)
        return None
    except Exception as exc:
        logger.error("Telegram %s failed: %s", method, type(exc).__name__)
        return None
    if not isinstance(body, dict) or not body.get("ok"):
        logger.error("Telegram %s was not accepted", method)
        return None
    return body.get("result", True)


def get_updates(db: Session, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]] | None:
    """One ``getUpdates`` long poll. ``None`` = failed or no bot token; ``[]`` = nothing new."""
    token = get_bot_token(db)
    if not token:
        return None
    payload: dict[str, Any] = {"timeout": timeout_seconds, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    # The HTTP timeout outlasts the long poll, or every quiet poll would look like a failure.
    result = _call_api(token, "getUpdates", payload, timeout=timeout_seconds + 10.0)
    if result is None:
        return None
    return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []


def delete_webhook(db: Session) -> bool:
    """Drop any webhook so ``getUpdates`` works (Telegram refuses both at once)."""
    token = get_bot_token(db)
    if not token:
        return False
    return _call_api(token, "deleteWebhook", {"drop_pending_updates": False}) is not None


def set_webhook(db: Session, url: str) -> bool:
    """Point Telegram at *url*, authenticated by the stored webhook secret."""
    token = get_bot_token(db)
    if not token:
        return False
    secret = ensure_webhook_secret(db)
    return (
        _call_api(token, "setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["message"]})
        is not None
    )


PAIRING_CODE_TTL_MINUTES = 15
_PAIRING_CODE_ALPHABET = string.ascii_uppercase + string.digits


def generate_pairing_code(db: Session, user_id: str) -> TelegramPairingCode:
    db.query(TelegramPairingCode).filter(TelegramPairingCode.user_id == user_id).delete()
    code = "".join(secrets.choice(_PAIRING_CODE_ALPHABET) for _ in range(6))
    pairing = TelegramPairingCode(
        user_id=user_id,
        code=code,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=PAIRING_CODE_TTL_MINUTES),
    )
    db.add(pairing)
    db.commit()
    db.refresh(pairing)
    return pairing


def link_pairing_code(db: Session, code: str, chat_id: str) -> TelegramAccount | None:
    pairing = db.query(TelegramPairingCode).filter(TelegramPairingCode.code == code).one_or_none()
    if pairing is None:
        return None
    expires_at = pairing.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(timezone.utc):
        db.delete(pairing)
        db.commit()
        return None

    account = db.query(TelegramAccount).filter(TelegramAccount.user_id == pairing.user_id).one_or_none()
    existing_other = db.query(TelegramAccount).filter(TelegramAccount.chat_id == chat_id).one_or_none()
    if existing_other is not None and existing_other.user_id != pairing.user_id:
        db.delete(existing_other)

    if account is None:
        account = TelegramAccount(user_id=pairing.user_id, chat_id=chat_id)
        db.add(account)
    else:
        account.chat_id = chat_id

    db.delete(pairing)
    db.commit()
    db.refresh(account)
    return account


def get_linked_account(db: Session, user_id: str) -> TelegramAccount | None:
    return db.query(TelegramAccount).filter(TelegramAccount.user_id == user_id).one_or_none()


def get_account_by_chat_id(db: Session, chat_id: str) -> TelegramAccount | None:
    return db.query(TelegramAccount).filter(TelegramAccount.chat_id == chat_id).one_or_none()


def ensure_webhook_secret(db: Session) -> str:
    secret = get_secret(db, "telegram")
    meta = secret[1] if secret else {}
    existing = meta.get("webhook_secret")
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
    # update_secret_meta is a safe no-op if no "telegram" secret row exists yet
    # (i.e. webhook-secret setup before a bot token is saved is a no-op, by design).
    update_secret_meta(db, "telegram", {"webhook_secret": token})
    return token


def verify_webhook_secret(db: Session, header_value: str | None) -> bool:
    if not header_value:
        return False
    secret = get_secret(db, "telegram")
    if not secret:
        return False
    stored = secret[1].get("webhook_secret")
    return bool(stored) and secrets.compare_digest(stored, header_value)


_FREEFORM_PATTERN = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s*$")


def parse_freeform_expense(text: str) -> tuple[Decimal, str] | None:
    match = _FREEFORM_PATTERN.match(text)
    if not match:
        return None
    raw_amount, description = match.groups()
    try:
        amount = Decimal(raw_amount.replace(",", "."))
    except InvalidOperation:
        return None
    if not description:
        return None
    return amount, description


def create_expense_from_telegram(db: Session, user_id: str, text: str) -> Expense | None:
    parsed = parse_freeform_expense(text)
    if parsed is None:
        return None
    amount, description = parsed
    category_id = categorize_description(db, user_id, description)
    expense = Expense(
        user_id=user_id,
        date=date.today(),
        amount=amount,
        currency="EUR",
        description=description,
        category_id=category_id,
        source="telegram",
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense
