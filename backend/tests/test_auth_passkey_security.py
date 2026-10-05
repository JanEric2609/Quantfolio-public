"""Regression tests for passkey authentication security fixes.

Covers three previously-exploitable weaknesses in app/api/auth.py:
  1. authenticate/verify issued a session cookie without a WebAuthn assertion
     when ``credential`` was omitted (account takeover from a known credential_id).
  2. authenticate/options enumerated every user's credential_id when called
     without a username (unauthenticated credential-id disclosure).
  3. logout only cleared the cookie; a captured JWT stayed valid until expiry.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.foundation.core.security import hash_password
from app.foundation.models.entities import PasskeyCredential, User
from app.foundation import auth as auth_service


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


@pytest.fixture
def client(db):
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_db, None)


def test_authenticate_verify_rejects_missing_assertion(client, db):
    """Omitting ``credential`` must NOT issue a session — the core bypass."""
    user = User(username="victim", password_hash=hash_password("Password123!"))
    db.add(user)
    db.commit()
    credential = PasskeyCredential(
        user_id=user.id, credential_id="known-cred-id", public_key="key", sign_count=0
    )
    db.add(credential)
    db.commit()

    challenge = "attacker-challenge"
    auth_service.challenge_store.put(challenge, {"kind": "authenticate", "user_id": None})

    resp = client.post(
        "/api/auth/passkey/authenticate/verify",
        json={"credential_id": "known-cred-id", "challenge": challenge, "sign_count": 999},
    )

    assert resp.status_code == 400
    assert "set-cookie" not in {k.lower() for k in resp.headers}


def test_authenticate_options_without_username_leaks_no_credentials(client, db):
    """No username → empty allowCredentials, never a dump of all users' ids."""
    for name in ("alice", "bob"):
        u = User(username=name, password_hash=hash_password("Password123!"))
        db.add(u)
        db.commit()
        db.add(PasskeyCredential(user_id=u.id, credential_id=f"{name}-cred", public_key="k"))
        db.commit()

    resp = client.post("/api/auth/passkey/authenticate/options", json={})

    assert resp.status_code == 200
    assert resp.json().get("allowCredentials", []) == []


def test_logout_revokes_outstanding_sessions(client, db):
    """Logout must bump session_version so an existing JWT is rejected afterwards."""
    user = User(
        username="jan", password_hash=hash_password("Password123!"), session_version=0
    )
    db.add(user)
    db.commit()

    login = client.post(
        "/api/auth/login", json={"username": "jan", "password": "Password123!"}
    )
    assert login.status_code == 200

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200

    db.refresh(user)
    assert user.session_version == 1
