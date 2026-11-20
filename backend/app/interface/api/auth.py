import base64
import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.core.config import get_settings
from app.foundation.core.db import get_db
from app.foundation.core.security import client_ip, limiter
from app.foundation.models.entities import PasskeyCredential, User
from app.foundation.schemas import (
    AuthSession,
    Message,
    PasskeyAuthenticateVerifyRequest,
    PasskeyCredentialOut,
    PasskeyOptionsRequest,
    PasskeyRegisterVerifyRequest,
    TokenResponse,
)
from app.foundation import auth as auth_service
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=8)


class RegisterCredentials(Credentials):
    # The one-time token the operator finds in the API log / data dir; see
    # foundation/setup_token.py. Empty is answered with a 403 that says so.
    setup_token: str = Field(default="", max_length=256)


class PasswordResetRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    owner_token: str = Field(min_length=1)


class PasswordResetConfirm(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    reset_token: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class PasswordResetTokenResponse(BaseModel):
    message: str
    reset_token: str


class PasskeyCredentialIn(BaseModel):
    credential_id: str
    public_key: str
    sign_count: int = 0
    name: str = "Passkey"


@router.get("/status")
def auth_status(db: Session = Depends(get_db)) -> dict:
    return {"initialized": auth_service.has_users(db)}


@router.post("/register", response_model=TokenResponse)
@limiter.limit("5/minute")
def register(
    request: Request, payload: RegisterCredentials, response: Response, db: Session = Depends(get_db)
) -> TokenResponse:
    user = auth_service.register_first_user(db, payload.username, payload.password, payload.setup_token)
    return TokenResponse(session=auth_service.issue_cookie(response, user))


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def login(request: Request, payload: Credentials, response: Response, db: Session = Depends(get_db)) -> TokenResponse:
    user = auth_service.authenticate_password(db, payload.username, payload.password, client_ip(request))
    return TokenResponse(session=auth_service.issue_cookie(response, user))


@router.post("/password-reset/request", response_model=PasswordResetTokenResponse)
@limiter.limit("3/hour")
def password_reset_request(
    request: Request,
    payload: PasswordResetRequest,
    db: Session = Depends(get_db),
) -> PasswordResetTokenResponse:
    """Step 1: Validate owner token, return a single-use minted token."""
    raw_token = auth_service.request_password_reset(
        db,
        username=payload.username,
        owner_token=payload.owner_token,
    )
    return PasswordResetTokenResponse(
        message="Password reset token minted",
        reset_token=raw_token,
    )


@router.post("/password-reset", response_model=Message)
@limiter.limit("3/hour")
def password_reset(request: Request, payload: PasswordResetConfirm, db: Session = Depends(get_db)) -> Message:
    auth_service.reset_password_with_token(
        db,
        username=payload.username,
        reset_token=payload.reset_token,
        new_password=payload.new_password,
    )
    return Message(message="Password reset")


@router.post("/logout", response_model=Message)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Message:
    # Bump session_version so any already-issued JWT (e.g. one captured from a
    # cookie) is rejected by current_user immediately, not just until expiry.
    user = auth_service.current_user_optional(request, db)
    if user is not None:
        user.session_version = (user.session_version or 0) + 1
        db.commit()
    auth_service.clear_cookie(response)
    return Message(message="Logged out")


@router.get("/me", response_model=TokenResponse)
def me(user: User = Depends(auth_service.current_user)) -> TokenResponse:
    return TokenResponse(
        session=AuthSession(
            user_id=user.id,
            username=user.username,
            generate_summary=auth_service.should_generate_summary(user),
        )
    )


@router.post("/passkey/register/options")
def passkey_register_options(
    payload: PasskeyOptionsRequest = PasskeyOptionsRequest(),
    user: User = Depends(auth_service.current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    webauthn = _webauthn_config(db)
    challenge_bytes = secrets.token_bytes(32)
    challenge = _b64url(challenge_bytes)
    auth_service.challenge_store.put(challenge, {"kind": "register", "user_id": user.id})
    try:
        from webauthn import generate_registration_options
        from webauthn.helpers import options_to_json_dict

        options = generate_registration_options(
            rp_id=webauthn["rp_id"],
            rp_name=webauthn["rp_name"],
            user_id=user.id.encode("utf-8"),
            user_name=user.username,
            user_display_name=user.username,
            challenge=challenge_bytes,
        )
        result = options_to_json_dict(options)
    except Exception:
        logger.error("WebAuthn registration options generation failed, using hand-built fallback", exc_info=True)
        result = {
            "rp": {"name": webauthn["rp_name"], "id": webauthn["rp_id"]},
            "user": {"id": user.id, "name": user.username, "displayName": user.username},
            "challenge": challenge,
            "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
            "timeout": 60000,
            "attestation": "none",
            "authenticatorSelection": {"residentKey": "preferred", "userVerification": "preferred"},
        }
    result["name"] = payload.name
    return result


@router.post("/passkey/register/verify", response_model=Message)
def passkey_register_verify(
    payload: PasskeyRegisterVerifyRequest,
    user: User = Depends(auth_service.current_user),
    db: Session = Depends(get_db),
) -> Message:
    challenge = auth_service.verify_passkey_challenge(payload.challenge, "register")
    if challenge.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="Passkey challenge does not belong to this user")
    # Require a verified attestation rather than trusting client-supplied
    # credential_id/public_key, so only keys the authenticator actually
    # generated (and proved with the challenge) can be stored.
    if not payload.credential:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passkey attestation is required",
        )
    try:
        from webauthn import verify_registration_response
        webauthn = _webauthn_config(db)

        verified = verify_registration_response(
            credential=payload.credential,
            expected_challenge=_b64url_decode(payload.challenge),
            expected_rp_id=webauthn["rp_id"],
            expected_origin=_expected_origins(webauthn["origin"]),
            require_user_verification=True,
        )
        credential_id = _b64url(verified.credential_id)
        public_key = _b64url(verified.credential_public_key)
        sign_count = verified.sign_count
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Passkey registration verification failed", exc_info=True)
        raise HTTPException(status_code=400, detail="Passkey registration verification failed") from exc
    if not credential_id or not public_key:
        raise HTTPException(status_code=400, detail="Passkey credential payload is incomplete")
    exists = (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.credential_id == credential_id)
        .one_or_none()
    )
    if exists:
        raise HTTPException(status_code=409, detail="Passkey already registered")
    db.add(
        PasskeyCredential(
            user_id=user.id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            name=payload.name,
            transports_json=json.dumps(payload.transports),
        )
    )
    db.commit()
    return Message(message="Passkey registered")


@router.post("/passkey/register", response_model=Message)
def passkey_register_compat(
    _payload: PasskeyCredentialIn,
    _user: User = Depends(auth_service.current_user),
    _db: Session = Depends(get_db),
) -> Message:
    raise HTTPException(
        status_code=410,
        detail="Legacy placeholder passkey registration is disabled. Use /passkey/register/options and /passkey/register/verify.",
    )


@router.post("/passkey/authenticate/options")
@limiter.limit("20/minute")
def passkey_authenticate_options(
    request: Request,
    payload: PasskeyOptionsRequest = PasskeyOptionsRequest(),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Start a passkey sign-in.

    Answers identically whether or not ``username`` exists, and never lists
    credential ids: the options used to return the user's credentials when the
    name was known and an empty list otherwise, which let anyone enumerate
    accounts. The browser finds the passkey itself (discoverable credentials);
    the challenge remembers which account was named so verification can still
    reject a credential that belongs to someone else.
    """
    webauthn = _webauthn_config(db)
    user = None
    if payload.username:
        user = db.query(User).filter(User.username == payload.username).one_or_none()

    challenge_bytes = secrets.token_bytes(32)
    challenge = _b64url(challenge_bytes)
    auth_service.challenge_store.put(
        challenge,
        {"kind": "authenticate", "user_id": user.id if user else None},
    )
    try:
        from webauthn import generate_authentication_options
        from webauthn.helpers import options_to_json_dict

        options = generate_authentication_options(
            rp_id=webauthn["rp_id"],
            challenge=challenge_bytes,
            allow_credentials=[],
        )
        return options_to_json_dict(options)
    except Exception:
        logger.error("WebAuthn authentication options generation failed, using hand-built fallback", exc_info=True)
        return {
            "challenge": challenge,
            "timeout": 60000,
            "rpId": webauthn["rp_id"],
            "userVerification": "preferred",
            "allowCredentials": [],
        }


@router.post("/passkey/authenticate/verify", response_model=TokenResponse)
@limiter.limit("5/minute")
def passkey_authenticate_verify(
    request: Request,
    payload: PasskeyAuthenticateVerifyRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    challenge = auth_service.verify_passkey_challenge(payload.challenge, "authenticate")
    credential = (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.credential_id == payload.credential_id)
        .one_or_none()
    )
    if credential is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown passkey")
    if challenge.get("user_id") and credential.user_id != challenge["user_id"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Passkey user mismatch")
    # A verified WebAuthn assertion is mandatory. Without it there is nothing
    # proving possession of the private key, so issuing a session would let any
    # caller who knows a credential_id (leaked freely by the options endpoint)
    # authenticate as that credential's owner. Fail closed.
    if not payload.credential:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passkey assertion is required",
        )
    try:
        from webauthn import verify_authentication_response
        webauthn = _webauthn_config(db)

        verified = verify_authentication_response(
            credential=payload.credential,
            expected_challenge=_b64url_decode(payload.challenge),
            expected_rp_id=webauthn["rp_id"],
            expected_origin=_expected_origins(webauthn["origin"]),
            credential_public_key=_b64url_decode(credential.public_key),
            credential_current_sign_count=credential.sign_count,
            require_user_verification=True,
        )
        new_sign_count = verified.new_sign_count
    except Exception as exc:
        logger.warning("Passkey authentication verification failed", exc_info=True)
        raise HTTPException(status_code=400, detail="Passkey authentication verification failed") from exc
    # WebAuthn L2 clone-detection: if authenticator supports counters (non-zero stored
    # OR non-zero new value), the new count must be strictly greater than stored.
    # If both are zero the authenticator does not support counters — skip detection.
    if new_sign_count != 0 or credential.sign_count != 0:
        if new_sign_count <= credential.sign_count:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Passkey sign count did not increase — possible cloned authenticator",
            )
    credential.sign_count = new_sign_count
    credential.last_used = datetime.now(UTC)
    user = db.get(User, credential.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    user.last_login_at = datetime.now(UTC)
    db.commit()
    return TokenResponse(session=auth_service.issue_cookie(response, user))


@router.post("/passkey/authenticate", response_model=TokenResponse)
@limiter.limit("5/minute")
def passkey_authenticate_compat(
    request: Request,
    payload: PasskeyAuthenticateVerifyRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    return passkey_authenticate_verify(request, payload, response, db)


@router.get("/passkey/credentials", response_model=list[PasskeyCredentialOut])
def list_passkeys(
    user: User = Depends(auth_service.current_user),
    db: Session = Depends(get_db),
) -> list[PasskeyCredential]:
    return (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.user_id == user.id)
        .order_by(PasskeyCredential.created_at.desc())
        .all()
    )


@router.get("/passkey/config")
def passkey_config(db: Session = Depends(get_db), _user: User = Depends(auth_service.current_user)) -> dict[str, Any]:
    return _webauthn_config(db)


@router.delete("/passkey/credentials/{credential_id}", response_model=Message)
def delete_passkey(
    credential_id: str,
    user: User = Depends(auth_service.current_user),
    db: Session = Depends(get_db),
) -> Message:
    credential = db.get(PasskeyCredential, credential_id)
    if credential is None:
        credential = (
            db.query(PasskeyCredential)
            .filter(PasskeyCredential.credential_id == credential_id)
            .one_or_none()
        )
    if credential is None or credential.user_id != user.id:
        raise HTTPException(status_code=404, detail="Passkey not found")
    db.delete(credential)
    db.commit()
    return Message(message="Passkey removed")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _expected_origins(origin_setting: str) -> list[str]:
    """``frontend_origin`` is a comma-separated list (LAN IP, .ts.net, ...);
    a passkey ceremony from any of them is valid for the configured rp_id."""
    return [o.strip() for o in origin_setting.split(",") if o.strip()]


def _webauthn_config(db: Session) -> dict[str, str]:
    settings = get_settings()
    public = get_public_settings(db)
    return {
        "rp_id": str(public.get("webauthn_rp_id") or settings.webauthn_rp_id),
        "rp_name": str(public.get("webauthn_rp_name") or settings.webauthn_rp_name),
        "origin": str(public.get("frontend_origin") or settings.frontend_origin),
    }
