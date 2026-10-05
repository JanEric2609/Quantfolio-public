import pytest
from conftest import _memory_db
from fastapi import HTTPException

from app.foundation.models.entities import User
from app.foundation.auth import require_admin


def _make_user(db, role: str = "user") -> User:
    """Create and persist a User with the given role."""
    user = User(username="testuser", password_hash="dummy", role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_admin_user_passes_dependency():
    """require_admin returns the user when role is 'admin'."""
    db = _memory_db()
    admin = _make_user(db, role="admin")
    result = require_admin(user=admin)
    assert result is admin
    assert result.role == "admin"


def test_non_admin_user_raises_403():
    """require_admin raises HTTPException 403 when role is not 'admin'."""
    db = _memory_db()
    user = _make_user(db, role="user")
    with pytest.raises(HTTPException) as exc:
        require_admin(user=user)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin role required"


def test_default_role_rejected():
    """require_admin rejects a user whose role is the default 'user'."""
    db = _memory_db()
    user = _make_user(db)  # role defaults to "user"
    with pytest.raises(HTTPException) as exc:
        require_admin(user=user)
    assert exc.value.status_code == 403


def test_empty_string_role_rejected():
    """require_admin rejects a user with an empty-string role."""
    db = _memory_db()
    user = _make_user(db, role="")
    with pytest.raises(HTTPException) as exc:
        require_admin(user=user)
    assert exc.value.status_code == 403


def test_unexpected_role_rejected():
    """require_admin rejects roles like 'moderator' or arbitrary strings."""
    for role in ("moderator", "editor", "Admin", "ADMIN", " admin "):
        db = _memory_db()
        user = _make_user(db, role=role)
        with pytest.raises(HTTPException) as exc:
            require_admin(user=user)
        assert exc.value.status_code == 403, f"role={role!r} should be rejected"


def test_admin_passes_with_arbitrary_other_fields():
    """require_admin only checks role; other user fields do not affect the check."""
    db = _memory_db()
    admin = _make_user(db, role="admin")
    admin.username = "someone_else"
    admin.risk_profile = "aggressive"
    result = require_admin(user=admin)
    assert result is admin
