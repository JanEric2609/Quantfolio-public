"""SMTP test-email API endpoint.

Exposes a single ``POST /api/email/test`` endpoint that sends a test
message via the configured SMTP server. Used by the Control Center to
verify that the user-supplied SMTP credentials actually work.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.email import send_test_email

router = APIRouter(prefix="/api/email", tags=["email"])


class TestEmailRequest(BaseModel):
    to_address: str


class TestEmailResponse(BaseModel):
    ok: bool
    message: str


@router.post("/test", response_model=TestEmailResponse)
def test_email(
    payload: TestEmailRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> TestEmailResponse:
    """Send a test email to verify the SMTP configuration."""
    ok, message = send_test_email(db, payload.to_address)
    return TestEmailResponse(ok=ok, message=message)
