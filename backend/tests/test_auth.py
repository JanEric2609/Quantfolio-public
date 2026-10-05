import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import _memory_db
from fastapi import HTTPException
from starlette.responses import Response

from app.interface.api.auth import _expected_origins, _webauthn_config, delete_passkey
from app.foundation.core.config import Settings
from app.foundation.core.security import hash_password, verify_password
from app.foundation.models.entities import PasskeyCredential, PasswordResetToken, User
from app.foundation.auth import issue_cookie, request_password_reset, reset_password_with_token
from app.foundation.settings import upsert_public_settings


def test_issue_cookie_uses_secure_flag_for_https_production(monkeypatch):
    settings = Settings(app_env="production", frontend_origin="https://quantfolio.local")
    monkeypatch.setattr("app.foundation.auth.get_settings", lambda: settings)

    user = SimpleNamespace(id="user-1", username="jan", last_executive_summary_generated=None)
    response = Response()

    issue_cookie(response, user)

    set_cookie = response.headers["set-cookie"].lower()
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie


def test_issue_cookie_uses_lax_for_local_http(monkeypatch):
    settings = Settings(app_env="local", frontend_origin="http://localhost:5173")
    monkeypatch.setattr("app.foundation.auth.get_settings", lambda: settings)

    user = SimpleNamespace(id="user-2", username="jan", last_executive_summary_generated=None)
    response = Response()

    issue_cookie(response, user)

    set_cookie = response.headers["set-cookie"].lower()
    assert "secure" not in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie


def test_passkey_delete_accepts_public_credential_id():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    credential = PasskeyCredential(user_id=user.id, credential_id="public-credential", public_key="key")
    db.add(credential)
    db.commit()

    result = delete_passkey("public-credential", user, db)

    assert result.message == "Passkey removed"
    assert db.query(PasskeyCredential).count() == 0


def test_webauthn_config_can_be_lan_overridden():
    db = _memory_db()
    upsert_public_settings(
        db,
        {
            "frontend_origin": "https://quantfolio.tailnet.test",
            "webauthn_rp_id": "quantfolio.tailnet.test",
        },
    )

    config = _webauthn_config(db)

    assert config["origin"] == "https://quantfolio.tailnet.test"
    assert config["rp_id"] == "quantfolio.tailnet.test"


def test_passkey_origins_accept_each_configured_frontend_origin():
    # Deployment docs recommend several origins (LAN IP + .ts.net); WebAuthn
    # used to compare the client origin against the whole CSV string.
    assert _expected_origins("http://10.0.0.23, https://qf.tailnet.ts.net,") == [
        "http://10.0.0.23",
        "https://qf.tailnet.ts.net",
    ]


def test_request_password_reset_mints_token(monkeypatch):
    db = _memory_db()
    user = User(username="jan", password_hash=hash_password("OldPassword123!"))
    db.add(user)
    db.commit()
    monkeypatch.setattr("app.foundation.auth.get_settings", lambda: Settings(password_reset_token="correct-token"))

    raw = request_password_reset(db, "jan", "correct-token")

    assert len(raw) > 20
    stored = db.query(PasswordResetToken).one()
    assert stored.user_id == user.id
    assert stored.used_at is None
    expires = stored.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    assert expires > datetime.now(UTC)


def test_password_reset_with_token_succeeds_once(monkeypatch):
    db = _memory_db()
    user = User(username="jan", password_hash=hash_password("OldPassword123!"))
    db.add(user)
    db.commit()
    monkeypatch.setattr("app.foundation.auth.get_settings", lambda: Settings(password_reset_token="correct-token"))

    raw = request_password_reset(db, "jan", "correct-token")
    result = reset_password_with_token(db, "jan", raw, "NewPassword123!")

    assert verify_password("NewPassword123!", result.password_hash)
    with pytest.raises(HTTPException) as exc:
        reset_password_with_token(db, "jan", raw, "AnotherPass123!")
    assert exc.value.status_code == 401


def test_password_reset_expired_token_rejected(monkeypatch):
    db = _memory_db()
    user = User(username="jan", password_hash=hash_password("OldPassword123!"))
    db.add(user)
    db.commit()
    monkeypatch.setattr("app.foundation.auth.get_settings", lambda: Settings(password_reset_token="correct-token"))

    token_hash = hashlib.sha256(b"expired-token").hexdigest()
    expired = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.add(expired)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        reset_password_with_token(db, "jan", "expired-token", "NewPassword123!")
    assert exc.value.status_code == 401


def test_password_reset_wrong_owner_token_returns_401(monkeypatch):
    db = _memory_db()
    db.add(User(username="jan", password_hash=hash_password("OldPassword123!")))
    db.commit()
    monkeypatch.setattr("app.foundation.auth.get_settings", lambda: Settings(password_reset_token="correct-token"))

    with pytest.raises(HTTPException) as exc:
        request_password_reset(db, "jan", "wrong-token")
    assert exc.value.status_code == 401


def test_password_reset_disabled_returns_403(monkeypatch):
    db = _memory_db()
    db.add(User(username="jan", password_hash=hash_password("OldPassword123!")))
    db.commit()
    monkeypatch.setattr("app.foundation.auth.get_settings", lambda: Settings(password_reset_token=None))

    with pytest.raises(HTTPException) as exc:
        request_password_reset(db, "jan", "token")
    assert exc.value.status_code == 403


def test_password_reset_bumps_session_version(monkeypatch):
    db = _memory_db()
    user = User(username="jan", password_hash=hash_password("OldPassword123!"), session_version=5)
    db.add(user)
    db.commit()
    monkeypatch.setattr("app.foundation.auth.get_settings", lambda: Settings(password_reset_token="correct-token"))

    raw = request_password_reset(db, "jan", "correct-token")
    result = reset_password_with_token(db, "jan", raw, "NewPassword123!")

    assert result.session_version == 6
