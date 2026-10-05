"""Telegram inbound updates: one handler, two delivery modes.

``handle_update`` is the single code path for a message from the bot, whether
it arrives as a webhook POST (``interface/api/telegram.py``) or from the
worker's ``getUpdates`` long poll (``worker.py:register_telegram_polling_job``).
It sits above ``telegram_bot`` (transport), ``telegram_replies`` and
``envelope`` so the channel adapter itself stays a leaf.

Polling keeps no public endpoint open: the worker asks Telegram for new
messages. The update offset is persisted in ``app_settings`` (internal state,
no catalog entry) so a restart neither replays nor skips messages. Nothing in
here logs a URL: the bot token is in it.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.foundation import telegram_bot
from app.foundation.envelope import check_envelope_threshold
from app.foundation.settings import get_public_settings, get_setting_row, upsert_public_settings
from app.foundation.telegram_bot import (
    create_expense_from_telegram,
    get_account_by_chat_id,
    get_bot_token,
    link_pairing_code,
)
from app.foundation.telegram_replies import format_balance_reply, format_summary_reply

logger = logging.getLogger(__name__)

UPDATE_OFFSET_KEY = "telegram_update_offset"
MODE_POLLING = "polling"
MODE_WEBHOOK = "webhook"

# The token whose webhook this process has already cleared (hash, not the token).
_webhook_cleared_for: str | None = None


def telegram_mode(db: Session) -> str:
    mode = str(get_public_settings(db).get("telegram_mode") or MODE_POLLING).strip().lower()
    return mode if mode in (MODE_POLLING, MODE_WEBHOOK) else MODE_POLLING


# -- Update handling (shared by webhook and polling) -------------------------


def handle_update(db: Session, update: dict[str, Any]) -> bool:
    """Act on one Telegram update. Returns True when it carried a text message."""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return False
    handle_message(db, chat_id, text)
    return True


def handle_message(db: Session, chat_id: str, text: str) -> None:
    # telegram_bot.send_telegram_message is looked up on the module at call time
    # (not imported by name) so tests can patch it in one place.
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            telegram_bot.send_telegram_message(db, chat_id, "Send /start followed by the pairing code shown in Settings.")
            return
        account = link_pairing_code(db, parts[1].strip(), chat_id)
        if account is None:
            telegram_bot.send_telegram_message(db, chat_id, "That pairing code is invalid or expired. Generate a new one in Settings.")
        else:
            telegram_bot.send_telegram_message(db, chat_id, "Linked! Send an amount + description (e.g. '3.50 coffee') to log an expense.")
        return

    account = get_account_by_chat_id(db, chat_id)
    if account is None:
        telegram_bot.send_telegram_message(db, chat_id, "This chat isn't linked yet. Generate a pairing code in Quantfolio Settings and send /start CODE.")
        return

    if text == "/balance":
        telegram_bot.send_telegram_message(db, chat_id, format_balance_reply(db, account.user_id))
        return
    if text == "/summary":
        telegram_bot.send_telegram_message(db, chat_id, format_summary_reply(db, account.user_id))
        return

    expense = create_expense_from_telegram(db, account.user_id, text)
    if expense is None:
        telegram_bot.send_telegram_message(db, chat_id, "Couldn't parse that. Send an amount + description, e.g. '3.50 coffee'.")
        return

    category_note = ""
    if expense.category_id is None:
        category_note = " (uncategorized — add a rule or categorize it in the Money tab)"
    telegram_bot.send_telegram_message(db, chat_id, f"Logged €{expense.amount} — {expense.description}{category_note}")

    if expense.category_id:
        try:
            check_envelope_threshold(db, account.user_id, expense.category_id, expense.date.year, expense.date.month)
        except Exception as exc:
            logger.error("Envelope threshold check failed for Telegram expense %s: %s", expense.id, exc)


# -- Webhook mode ------------------------------------------------------------


def sync_webhook(db: Session) -> bool:
    """Make webhook mode usable: make sure the secret exists, then register the URL.

    ``ensure_webhook_secret`` was never called before, so the webhook endpoint
    failed closed forever. Registration with Telegram needs an https origin
    (from ``frontend_origin``); without one only the secret is prepared.
    """
    from app.foundation.settings import resolve_frontend_origins

    if not get_bot_token(db):
        return False
    telegram_bot.ensure_webhook_secret(db)
    origin = next((o for o in resolve_frontend_origins(db) if o.lower().startswith("https://")), None)
    if origin is None:
        logger.warning("Telegram webhook mode needs an https origin in frontend_origin; webhook not registered")
        return False
    return telegram_bot.set_webhook(db, f"{origin.rstrip('/')}/api/telegram/webhook")


# -- Polling mode ------------------------------------------------------------


def _stored_offset(db: Session) -> int | None:
    value = get_setting_row(db, UPDATE_OFFSET_KEY)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _store_offset(db: Session, offset: int) -> None:
    upsert_public_settings(db, {UPDATE_OFFSET_KEY: offset})


def _ensure_webhook_cleared(db: Session) -> bool:
    """``deleteWebhook`` once per bot token and process; getUpdates fails while one is set."""
    global _webhook_cleared_for
    token = get_bot_token(db)
    if not token:
        return False
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if _webhook_cleared_for == fingerprint:
        return True
    if not telegram_bot.delete_webhook(db):
        return False
    _webhook_cleared_for = fingerprint
    return True


def poll_once(db: Session, *, long_poll_seconds: int = 20) -> int | None:
    """One long poll. Returns the number of updates handled, ``None`` when nothing was polled."""
    global _webhook_cleared_for
    if telegram_mode(db) != MODE_POLLING or not get_bot_token(db):
        return None
    if not _ensure_webhook_cleared(db):
        return None

    updates = telegram_bot.get_updates(db, _stored_offset(db), long_poll_seconds)
    if updates is None:
        # A webhook set elsewhere makes getUpdates fail; clear it again next run.
        _webhook_cleared_for = None
        return None

    handled = 0
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            # Persist before acting: a crash mid-handler then drops one message
            # (the sender sees no reply and resends) instead of logging an
            # expense twice.
            _store_offset(db, update_id + 1)
        try:
            if handle_update(db, update):
                handled += 1
        except Exception as exc:
            logger.error("Telegram update handling failed: %s", type(exc).__name__, exc_info=True)
    return handled


def poll_telegram_updates(
    db: Session,
    *,
    budget_seconds: float = 50.0,
    long_poll_seconds: int = 20,
) -> dict[str, int]:
    """Long-poll repeatedly for about *budget_seconds* (one scheduler tick).

    A quiet poll returns after ``long_poll_seconds``; an update returns at once,
    so the loop keeps the bot responsive between scheduler ticks. A skipped or
    failed poll ends the run — the next tick retries.
    """
    deadline = time.monotonic() + budget_seconds
    handled = 0
    polls = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 3:
            break
        started = time.monotonic()
        result = poll_once(db, long_poll_seconds=int(min(long_poll_seconds, remaining - 2)))
        if result is None:
            break
        polls += 1
        handled += result
        if time.monotonic() - started < 1.0:
            # An answer that comes back at once and carries nothing new must not
            # turn the loop into a hot spin against the Bot API.
            time.sleep(1.0)
    return {"polls": polls, "handled": handled}
