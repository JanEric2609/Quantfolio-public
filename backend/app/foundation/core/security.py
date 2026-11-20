import base64
import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
from cryptography.fernet import Fernet, InvalidToken
from slowapi import Limiter

from app.foundation.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def client_ip(request: Any) -> str:
    """The address the request really came from.

    Behind Caddy every connection arrives from the proxy, so ``request.client``
    is only the client when uvicorn runs with ``--proxy-headers`` and
    ``--forwarded-allow-ips`` naming the proxy (infra/systemd/quantfolio-api.service
    does); uvicorn then rewrites ``scope["client"]`` from X-Forwarded-For. The
    header itself is never read here: trusting it without the allow-list would
    let any caller pick their own rate-limit and lockout bucket.
    """
    client = getattr(request, "client", None)
    return (getattr(client, "host", None) or "unknown") if client else "unknown"


# Rate limiter instance — shared by main.py and API routers to avoid circular imports.
limiter = Limiter(key_func=client_ip)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return f"pbkdf2_sha256${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = password_hash.split("$", 2)
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.b64decode(salt_b64), 260_000
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_token(subject: str, claims: dict[str, Any] | None = None, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "sub": subject,
        "iat": now,
        "exp": int((datetime.now(UTC) + timedelta(hours=settings.jwt_expiry_hours)).timestamp()),
        **(claims or {}),
    }
    return pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        return pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise ValueError("Token expired")
    except pyjwt.InvalidTokenError as exc:
        raise ValueError(str(exc))


# Fixed, non-secret salt + info label for the dev-only HKDF derivation. The
# label domain-separates the Fernet key from any other use of the JWT secret so
# the two are not the same bytes (issue #129). Not used when ENCRYPTION_KEY is set.
_DEV_FERNET_HKDF_SALT = b"quantfolio.secretbox.hkdf.salt.v1"
_DEV_FERNET_HKDF_INFO = b"quantfolio-secretbox-fernet-v1"


def _derive_dev_fernet_key(jwt_secret: str) -> bytes:
    """Deterministically derive 32 raw key bytes from the JWT secret via HKDF-SHA256."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_DEV_FERNET_HKDF_SALT,
        info=_DEV_FERNET_HKDF_INFO,
    )
    return hkdf.derive(jwt_secret.encode("utf-8"))


@dataclass
class SecretBox:
    fernet: Fernet

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "SecretBox":
        settings = settings or get_settings()
        if settings.encryption_key:
            key = settings.encryption_key.encode()
        else:
            # Fail closed: the JWT-derived fallback is dev-only and must never run
            # against a real deployment. A Postgres DSN is a production signal
            # regardless of APP_ENV (issue #129).
            if not settings.is_local or settings.is_postgres:
                raise RuntimeError(
                    "ENCRYPTION_KEY must be set in non-local environments. "
                    "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
                )
            # Local/dev fallback only — derives the Fernet key from the JWT secret
            # via HKDF with a fixed salt + domain-separation label, so the two
            # cryptographic domains are not trivially interchangeable. Never used
            # in production. WARNING logged so it is visible in dev.
            logger.warning(
                "ENCRYPTION_KEY is not set; deriving a dev-only Fernet key from the JWT secret. "
                "Set ENCRYPTION_KEY in your environment for any non-local deployment."
            )
            key = base64.urlsafe_b64encode(_derive_dev_fernet_key(settings.jwt_secret))
        return cls(Fernet(key))

    def encrypt(self, plaintext: str) -> str:
        return self.fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self.fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Unable to decrypt stored secret with current ENCRYPTION_KEY") from exc
