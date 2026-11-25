"""Telegram bot integration: pairing, outbound delivery, freeform entry parsing.

Money Phase 3 (docs/archive/plans/student-budget-redesign.md §1.6, §1.7).
"""

import logging
import re
import secrets
import string
import time
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


#: Telegram refuses a text over 4096 characters (HTTP 400), which used to drop the whole message.
MAX_TEXT = 4096
#: A 429 names ``retry_after`` seconds. One call (all parts of a split message together) waits at
#: most this long in total; a longer ask is not slept through, the message is given up on.
#: ``getUpdates`` never sleeps (its next poll is soon). One-shot alerts keep the old 30 s window.
MAX_RETRY_WAIT_S = 30.0


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Cut *text* into parts of at most *limit* characters, at a line break when there is one."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:  # no usable line break: a hard cut
            cut = limit
        parts.append(rest[:cut].rstrip("\n"))
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts


def _api_error(response: Any) -> tuple[str, float | None]:
    """Telegram's own ``description`` (never contains the token) and ``retry_after`` from an error response."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an error body that is not JSON is only less informative
        return "", None
    if not isinstance(body, dict):
        return "", None
    params = body.get("parameters")
    retry = params.get("retry_after") if isinstance(params, dict) else None
    retry = float(retry) if isinstance(retry, (int, float)) and not isinstance(retry, bool) else None
    return str(body.get("description") or "")[:200], retry


def _post_once(client: Any, url: str, payload: dict[str, Any], budget: list[float] | None = None) -> Any:
    """POST; on a 429 wait the ``retry_after`` it names and post once more.

    *budget* is the seconds this call may still sleep in total (one element, shared by the parts of
    a split message and reduced here). A wait that does not fit is not slept: the 429 is raised.
    Without a budget the default ``MAX_RETRY_WAIT_S`` applies.
    """
    response = client.post(url, json=payload)
    if getattr(response, "status_code", None) == 429:
        left = budget if budget is not None else [MAX_RETRY_WAIT_S]
        _, retry = _api_error(response)
        wait = max(retry if retry is not None else 1.0, 0.0)
        if wait <= left[0]:
            left[0] -= wait
            logger.warning("Telegram rate limit: retrying in %.0f s", wait)
            time.sleep(wait)
            response = client.post(url, json=payload)
        else:
            logger.warning("Telegram rate limit: asked to wait %.0f s, more than the %.0f s left; not retrying",
                           wait, left[0])
    response.raise_for_status()
    return response


def send_telegram_message(
    db: Session, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None,
    *, preview: bool = True,
) -> bool:
    """Send *text*; a long one goes out in parts (the buttons under the last).

    ``True`` when the message reached the chat. If a later part of a split message fails, the
    earlier parts are already delivered: that is still ``True`` (a caller that retried would send
    them twice), the loss is logged, and a short notice that the rest is missing is sent on a
    best-effort basis. ``False`` only when nothing was delivered.
    """
    token = get_bot_token(db)
    if not token:
        logger.warning("Telegram message not sent: no bot token configured")
        return False
    _silence_http_loggers()
    parts = split_text(text)
    delivered = 0
    budget = [MAX_RETRY_WAIT_S]
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    try:
        with httpx.Client(timeout=10.0) as client:
            try:
                for i, part in enumerate(parts):
                    markup = reply_markup if i == len(parts) - 1 else None
                    _post_once(client, url,
                               {"chat_id": chat_id, "text": part, **({"reply_markup": markup} if markup else {}),
                                **({} if preview else {"disable_web_page_preview": True})},
                               budget)
                    delivered += 1
            except Exception:
                if delivered:
                    logger.error("Telegram message delivered in part: %d of %d parts sent", delivered, len(parts))
                    try:
                        client.post(url, json={"chat_id": chat_id,
                                               "text": f"(The rest of this message could not be delivered: "
                                                       f"{delivered} of {len(parts)} parts arrived.)"})
                    except Exception:  # noqa: BLE001 - best effort, the loss is already logged
                        pass
                raise
        return True
    except httpx.HTTPStatusError as exc:
        # Do not log str(exc)/the exception object: httpx.HTTPStatusError's
        # default message embeds the full request URL, which contains the
        # bot token (Telegram authenticates via URL path, not headers).
        description, _ = _api_error(exc.response)
        logger.error(
            "Telegram sendMessage failed: HTTP %s from Telegram API%s",
            exc.response.status_code, f" ({description})" if description else "",
        )
        return delivered > 0
    except Exception as exc:
        logger.error("Telegram sendMessage failed: %s", type(exc).__name__)
        return delivered > 0


def _fit(text: str) -> str:
    """One message that is edited later cannot be split: cut it to the limit with an ellipsis."""
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def send_telegram_message_id(
    db: Session, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None
) -> int | None:
    """Like :func:`send_telegram_message`, but returns the sent message's id (to edit it later), or None."""
    token = get_bot_token(db)
    if not token:
        logger.warning("Telegram message not sent: no bot token configured")
        return None
    payload: dict[str, Any] = {"chat_id": chat_id, "text": _fit(text)}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = _call_api(token, "sendMessage", payload)
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None


def edit_telegram_message(
    db: Session, chat_id: str, message_id: int, text: str, reply_markup: dict[str, Any] | None = None
) -> bool:
    """Replace a sent message's text and buttons. Without *reply_markup* the buttons are removed."""
    token = get_bot_token(db)
    if not token:
        return False
    payload = {"chat_id": chat_id, "message_id": message_id, "text": _fit(text),
               "reply_markup": reply_markup if reply_markup else {"inline_keyboard": []}}
    return _call_api(token, "editMessageText", payload) is not None


def _call_api(token: str, method: str, payload: dict[str, Any] | None = None, *, timeout: float = 10.0) -> Any | None:
    """POST one Bot API method; the decoded ``result`` on success, ``None`` on any failure.

    The bot token is part of the request URL, so nothing here may log the URL,
    the request, or an exception object (``httpx`` embeds the URL in both): only
    the method name and a status code / exception class go to the log.
    """
    _silence_http_loggers()
    try:
        with httpx.Client(timeout=timeout) as client:
            # a long poll never sleeps through a 429: the next poll comes soon anyway
            response = _post_once(client, f"{TELEGRAM_API_BASE}/bot{token}/{method}", payload or {},
                                  [0.0] if method == "getUpdates" else None)
            body = response.json()
    except httpx.HTTPStatusError as exc:
        description, _ = _api_error(exc.response)
        logger.error("Telegram %s failed: HTTP %s from Telegram API%s", method, exc.response.status_code,
                     f" ({description})" if description else "")
        return None
    except httpx.TimeoutException as exc:
        # About 2 % of long polls time out on the network (audit 2026-11-15,
        # 2.30); the next poll picks up where this one stopped, so a poll
        # timeout is no error. Any other method's timeout still is.
        log = logger.info if method == "getUpdates" else logger.error
        log("Telegram %s failed: %s", method, type(exc).__name__)
        return None
    except Exception as exc:
        logger.error("Telegram %s failed: %s", method, type(exc).__name__)
        return None
    if not isinstance(body, dict) or not body.get("ok"):
        logger.error("Telegram %s was not accepted", method)
        return None
    return body.get("result", True)


ALLOWED_UPDATES = ("message", "callback_query")


def answer_callback_query(db: Session, callback_id: str, text: str) -> bool:
    """Answer a button tap so the client stops its spinner; *text* shows as a short toast."""
    token = get_bot_token(db)
    if not token:
        return False
    return _call_api(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]}) is not None


def get_updates(db: Session, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]] | None:
    """One ``getUpdates`` long poll. ``None`` = failed or no bot token; ``[]`` = nothing new."""
    token = get_bot_token(db)
    if not token:
        return None
    payload: dict[str, Any] = {"timeout": timeout_seconds, "allowed_updates": list(ALLOWED_UPDATES)}
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
        _call_api(token, "setWebhook", {"url": url, "secret_token": secret, "allowed_updates": list(ALLOWED_UPDATES)})
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
    # Codes are upper case; a phone keyboard often sends them lower case.
    code = code.strip().upper()
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
        # A new link: the settings card watches this to see its code was used.
        account.linked_at = datetime.now(UTC)

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
