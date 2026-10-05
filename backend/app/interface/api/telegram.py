import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.schemas import TelegramLinkStatusOut, TelegramPairingCodeOut
from app.foundation.auth import current_user
from app.foundation.telegram_bot import (
    generate_pairing_code,
    get_linked_account,
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


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if not verify_webhook_secret(db, x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()
    try:
        await run_in_threadpool(handle_update, db, update)
    except Exception as exc:
        logger.error("Telegram webhook handling failed: %s", exc, exc_info=True)
    return {"ok": True}
