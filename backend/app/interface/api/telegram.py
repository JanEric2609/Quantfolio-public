import logging
import threading
from collections import deque
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.schemas import TelegramLinkStatusOut, TelegramPairingCodeOut, TelegramTestMessageOut
from app.foundation.auth import current_user
from app.foundation.telegram_bot import (
    generate_pairing_code,
    get_bot_token,
    get_linked_account,
    send_telegram_message,
    verify_webhook_secret,
)
from app.foundation.telegram_updates import handle_update

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.post("/pairing-code", response_model=TelegramPairingCodeOut)
def create_pairing_code(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    pairing = generate_pairing_code(db, user.id)
    return {"code": pairing.code, "expires_at": pairing.expires_at.isoformat()}


@router.get("/link-status", response_model=TelegramLinkStatusOut)
def link_status(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    account = get_linked_account(db, user.id)
    if account is None:
        return {"linked": False}
    return {"linked": True, "linked_at": account.linked_at.isoformat()}


@router.post("/test-message", response_model=TelegramTestMessageOut)
def send_test_message(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    """Send one message to the caller's linked chat, so alerts are known to arrive."""
    if not get_bot_token(db):
        return {"sent": False, "message": "No bot token saved."}
    account = get_linked_account(db, user.id)
    if account is None:
        return {"sent": False, "message": "No chat linked yet. Generate a pairing code first."}
    text = "Quantfolio test message: alerts from the daily self-check and the crash guard will arrive here."
    if send_telegram_message(db, account.chat_id, text):
        return {"sent": True, "message": "Sent. Check Telegram."}
    return {"sent": False, "message": "Telegram did not accept the message. Test the bot token above."}


#: Update ids this process handled recently: Telegram re-delivers an update whose webhook answered late.
_SEEN_UPDATES: deque[int] = deque(maxlen=500)
_SEEN_LOCK = threading.Lock()


def _first_delivery(update: dict[str, Any]) -> bool:
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return True
    with _SEEN_LOCK:
        if update_id in _SEEN_UPDATES:
            return False
        _SEEN_UPDATES.append(update_id)
    return True


def _handle_after_response(bind: Any, update: dict[str, Any]) -> None:
    """Run after the 200 went out: a desk /idea makes about twenty market reads, longer than Telegram
    waits before it re-delivers the update (and the idea would be proposed twice). Its own session on
    the request's engine: the request's session is closed by then."""
    db = Session(bind=bind, autoflush=False)
    try:
        handle_update(db, update)
    except Exception as exc:  # noqa: BLE001 - a webhook must never fail; the error is logged
        logger.error("Telegram webhook handling failed: %s", exc, exc_info=True)
    finally:
        db.close()


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if not verify_webhook_secret(db, x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()
    if isinstance(update, dict) and _first_delivery(update):
        background.add_task(_handle_after_response, db.get_bind(), update)
    return {"ok": True}
