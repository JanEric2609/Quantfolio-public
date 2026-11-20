from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.schemas import SetupStatus
from app.foundation.auth import current_user_optional, has_users, passkey_count
from app.foundation.settings import get_public_settings, get_secret, integration_presence
from app.foundation.models.entities import User

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status", response_model=SetupStatus)
def setup_status(
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user_optional),
) -> SetupStatus:
    initialized = has_users(db)
    # Unauthenticated callers learn nothing beyond "setup required" (no users yet)
    # or "already initialized": no integration presence, no passkey count. A fresh
    # instance also answers with the bare minimum, since the account step is gated
    # by the one-time setup token rather than by secrecy of the status.
    if user is None:
        return SetupStatus(
            initialized=initialized,
            has_passkey=False,
            integrations={},
            next_step="" if initialized else "account",
        )
    has_passkey = passkey_count(db) > 0
    integrations = integration_presence(db)
    public = get_public_settings(db)
    secret = get_secret(db, "dkb")
    dkb_meta = secret[1] if secret else {}
    if integrations.get("dkb") and public.get("dkb_provider", "fints") == "fints":
        integrations["dkb"] = bool(public.get("dkb_product_id") or dkb_meta.get("product_id"))
    if not has_passkey:
        next_step = "passkey"
    elif not integrations.get("llm"):
        next_step = "llm"
    elif not integrations.get("dkb"):
        next_step = "dkb"
    elif not integrations.get("alphavantage"):
        next_step = "alphavantage"
    elif not integrations.get("finnhub"):
        next_step = "finnhub"
    else:
        next_step = "complete"
    return SetupStatus(
        initialized=initialized,
        has_passkey=has_passkey,
        integrations=integrations,
        next_step=next_step,
    )
