import hashlib
import hmac
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.core.config import get_settings
from app.foundation.core.db import get_db
from app.foundation.core.security import create_token, decode_token, hash_password, verify_password
from app.foundation.models.entities import PasskeyCredential, PasswordResetToken, Portfolio, User
from app.foundation.schemas import AuthSession
from app.foundation.setup_token import discard_setup_token, verify_setup_token


COOKIE_NAME = "quantfolio_session"
PASSKEY_CHALLENGE_TTL_SECONDS = 300
CHALLENGE_STORE_MAX_SIZE = 1000

# Failures per (username, client IP) before that pair is locked out. Keyed on the
# pair, not the username alone, so someone hammering "jan" from elsewhere cannot
# lock the real jan out from home.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 900
# Ceiling on failures per username across ALL addresses, so distributed guessing
# (many IPs, one account) is still slowed. Far above what a typo-prone owner
# reaches, far below what a botnet wants.
LOGIN_USERNAME_MAX_ATTEMPTS = 50

_UNKNOWN_CLIENT = "unknown"
# Bound on tracked keys; a sweep drops expired ones so a flood of made-up
# usernames cannot grow the dict without limit.
_TRACKER_SWEEP_THRESHOLD = 10_000


class LoginTracker:
    """In-memory sliding-window login failure counter.

    Two counters per failure: one for the (username, client IP) pair, one for
    the username alone. ``is_blocked`` trips on either ceiling.
    """

    def __init__(self) -> None:
        self._attempts: dict[str, list[datetime]] = {}

    @staticmethod
    def _pair_key(username: str, ip: str | None) -> str:
        return f"{username}\x00{ip or _UNKNOWN_CLIENT}"

    def _prune(self, key: str) -> list[datetime]:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=LOGIN_WINDOW_SECONDS)
        attempts = self._attempts.get(key, [])
        pruned = [t for t in attempts if t > cutoff]
        if pruned:
            self._attempts[key] = pruned
        else:
            self._attempts.pop(key, None)
        return pruned

    def _sweep(self) -> None:
        if len(self._attempts) < _TRACKER_SWEEP_THRESHOLD:
            return
        for key in list(self._attempts):
            self._prune(key)

    def record_failure(self, username: str, ip: str | None = None) -> None:
        self._sweep()
        now = datetime.now(UTC)
        for key in (username, self._pair_key(username, ip)):
            self._prune(key)
            self._attempts.setdefault(key, []).append(now)

    def is_blocked(self, username: str, ip: str | None = None) -> bool:
        if len(self._prune(self._pair_key(username, ip))) >= LOGIN_MAX_ATTEMPTS:
            return True
        return len(self._prune(username)) >= LOGIN_USERNAME_MAX_ATTEMPTS

    def clear(self, username: str, ip: str | None = None) -> None:
        """Forget failures after a success.

        With an address: only that (username, IP) pair. Without one: everything
        recorded for the username. The per-username total is otherwise left to
        expire, so a successful login cannot reset an attacker's budget.
        """
        if ip is not None:
            self._attempts.pop(self._pair_key(username, ip), None)
            return
        self._attempts.pop(username, None)
        prefix = f"{username}\x00"
        for key in [k for k in self._attempts if k.startswith(prefix)]:
            self._attempts.pop(key, None)


login_tracker = LoginTracker()


class ChallengeStore:
    def __init__(self) -> None:
        self._memory: dict[str, tuple[datetime, dict[str, Any]]] = {}

    def _redis(self):
        try:
            import redis
        except Exception:
            return None
        try:
            client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
            client.ping()
            return client
        except Exception:
            return None

    def put(self, challenge: str, payload: dict[str, Any]) -> None:
        client = self._redis()
        if client:
            client.setex(
                f"quantfolio:passkey:{challenge}",
                PASSKEY_CHALLENGE_TTL_SECONDS,
                json.dumps(payload),
            )
            return
        # Bound in-memory fallback: evict oldest entry if at capacity
        if len(self._memory) >= CHALLENGE_STORE_MAX_SIZE:
            oldest_key = next(iter(self._memory))
            del self._memory[oldest_key]
        self._memory[challenge] = (
            datetime.now(UTC) + timedelta(seconds=PASSKEY_CHALLENGE_TTL_SECONDS),
            payload,
        )

    def pop(self, challenge: str) -> dict[str, Any] | None:
        client = self._redis()
        if client:
            key = f"quantfolio:passkey:{challenge}"
            raw = client.getdel(key)
            return json.loads(raw) if raw else None
        item = self._memory.pop(challenge, None)
        if item is None:
            return None
        expires_at, payload = item
        if expires_at < datetime.now(UTC):
            return None
        return payload


challenge_store = ChallengeStore()


def has_users(db: Session) -> bool:
    return db.query(User.id).first() is not None


# Serialises first-user registration inside this process; on PostgreSQL the
# advisory lock below extends that to every process sharing the database.
_FIRST_USER_LOCK = threading.Lock()


def _lock_first_user_registration(db: Session) -> None:
    """Hold a transaction-scoped lock so the has-users check and the insert are one step."""
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('quantfolio:first-user'))"))


def register_first_user(db: Session, username: str, password: str, setup_token: str | None) -> User:
    """Create the first (admin) account.

    Only possible while no user exists, and only with the one-time setup token
    (``foundation/setup_token.py``): a fresh or wiped instance must not belong
    to whoever reaches the registration form first.
    """
    if has_users(db):
        raise HTTPException(status_code=409, detail="QuantFolio is already initialized")
    if not setup_token:
        raise HTTPException(status_code=403, detail="A setup token is required to create the first account")
    if not verify_setup_token(setup_token):
        raise HTTPException(status_code=403, detail="Invalid setup token")

    password_hash = hash_password(password)  # slow; keep it outside the lock
    with _FIRST_USER_LOCK:
        _lock_first_user_registration(db)
        if has_users(db):
            db.rollback()
            raise HTTPException(status_code=409, detail="QuantFolio is already initialized")
        user = User(username=username, password_hash=password_hash, role="admin")
        db.add(user)
        try:
            db.flush()
            db.add(Portfolio(user_id=user.id, name="Main Portfolio", currency="EUR"))
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="QuantFolio is already initialized") from exc
    db.refresh(user)
    discard_setup_token()
    return user


# A valid hash of a password nobody knows, verified against when the username
# does not exist so an unknown user costs the same PBKDF2 run as a wrong password.
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$3H1J2WYCsxVjKkF//xyIUw==$IYiT8LqV5TOFh3Cn/mQF4UqCahi91S2Kv82toFs+x/0="
)


def authenticate_password(db: Session, username: str, password: str, client_ip: str | None = None) -> User:
    if login_tracker.is_blocked(username, client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later.",
        )
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        # Burn the same PBKDF2 time as a real check, so response time does not
        # tell an attacker which usernames exist.
        verify_password(password, _DUMMY_PASSWORD_HASH)
        login_tracker.record_failure(username, client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not verify_password(password, user.password_hash):
        login_tracker.record_failure(username, client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    login_tracker.clear(username, client_ip)
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


def request_password_reset(db: Session, username: str, owner_token: str) -> str:
    """Step 1: Verify owner token, mint a single-use time-limited reset token.

    Returns the raw minted token (shown once to the caller). The owner token
    is the static PASSWORD_RESET_TOKEN env secret — this function validates it
    and then creates a one-time token in the database.
    """
    settings = get_settings()
    configured_token = settings.password_reset_token
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password reset is disabled. Set PASSWORD_RESET_TOKEN in the backend environment.",
        )
    token_valid = hmac.compare_digest(owner_token, configured_token)
    user = db.query(User).filter(User.username == username).one_or_none()
    if not token_valid or user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password reset token")

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(minutes=settings.reset_token_expire_minutes),
    )
    db.add(reset)
    db.commit()
    return raw_token


def reset_password_with_token(db: Session, username: str, reset_token: str, new_password: str) -> User:
    """Step 2: Redeem a single-use reset token to change password."""
    token_hash = hashlib.sha256(reset_token.encode()).hexdigest()
    reset = db.query(PasswordResetToken).filter(
        PasswordResetToken.token_hash == token_hash
    ).one_or_none()
    if reset is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired reset token")
    if reset.used_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Reset token already used")
    now = datetime.now(UTC)
    expires = reset.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Reset token expired")

    user = db.get(User, reset.user_id)
    if user is None or user.username != username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired reset token")

    user.password_hash = hash_password(new_password)
    user.session_version = (user.session_version or 0) + 1
    reset.used_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


def should_generate_summary(user: User) -> bool:
    today = datetime.now(UTC).date()
    return user.last_executive_summary_generated != today


def _cookie_secure(settings) -> bool:
    """Return True when the session cookie should be sent over HTTPS only.

    Respects the explicit ``cookie_secure`` config value; local environments
    default to False so that HTTP dev servers work out of the box.
    """
    if settings.is_local:
        return False
    return settings.cookie_secure


def issue_cookie(response: Response, user: User) -> AuthSession:
    session = AuthSession(
        user_id=user.id,
        username=user.username,
        generate_summary=should_generate_summary(user),
    )
    token = create_token(user.id, {"username": user.username, "session_version": getattr(user, "session_version", 0)})
    settings = get_settings()
    secure = _cookie_secure(settings)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=60 * 60 * 24,
        path="/",
    )
    return session


def verify_passkey_challenge(challenge: str, kind: str) -> dict[str, Any]:
    payload = challenge_store.pop(challenge)
    if not payload or payload.get("kind") != kind:
        raise HTTPException(status_code=400, detail="Passkey challenge is invalid or expired")
    return payload


def clear_cookie(response: Response) -> None:
    settings = get_settings()
    secure = _cookie_secure(settings)
    # Mirror all Set-Cookie attributes so the browser actually expires the cookie.
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Return the current user or None if not authenticated.

    Unlike ``current_user``, this dependency does not raise 401 on missing/invalid tokens.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = decode_token(token)
    except ValueError:
        return None
    user = db.get(User, payload["sub"])
    if user is None:
        return None
    if int(payload.get("session_version", -1)) != (user.session_version or 0):
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from exc
    user = db.get(User, payload["sub"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    if int(payload.get("session_version", -1)) != (user.session_version or 0):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session has been revoked")
    return user


def passkey_count(db: Session) -> int:
    return db.query(PasskeyCredential.id).count()


def require_admin(user: User = Depends(current_user)) -> User:
    """FastAPI dependency that enforces the authenticated user has admin role."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user
