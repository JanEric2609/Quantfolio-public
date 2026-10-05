"""Jurisdictions route auth guard (audit-fixes-2026-08 todo 35, audit §11).

``GET /api/tax/jurisdictions`` was the only tax route without the standard
session/current-user dependency. These tests pin the corrected contract:

1. Unauthenticated request (no session cookie, real ``current_user``
   dependency — NOT overridden) must be rejected with 401.
2. Authenticated requests keep working and still return the jurisdictions
   payload.
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import User
from app.foundation.auth import current_user


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _client(*, authenticated: bool) -> TestClient:
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    if authenticated:
        app.dependency_overrides[current_user] = lambda: user
    # When not authenticated the REAL current_user dependency runs: no cookie
    # in the TestClient request means it must raise 401.
    return TestClient(app)


def test_jurisdictions_rejects_unauthenticated_request():
    client = _client(authenticated=False)

    response = client.get("/api/tax/jurisdictions")

    assert response.status_code == 401


def test_jurisdictions_serves_payload_when_authenticated():
    client = _client(authenticated=True)

    response = client.get("/api/tax/jurisdictions")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload.get("jurisdictions"), list)
    assert len(payload["jurisdictions"]) > 0
