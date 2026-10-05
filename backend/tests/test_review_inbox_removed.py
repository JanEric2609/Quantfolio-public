"""The review-inbox API must be extinct (audit-fixes-2026-08 todo 8).

GET /api/review/inbox must 404 — the router was deleted, not merely gated.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_review_inbox_routes_are_gone():
    from app.main import app
    from app.foundation.core.db import get_db
    from app.foundation.models.entities import User
    from app.foundation.auth import current_user

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app)
        assert client.get("/api/review/inbox").status_code == 404
        assert client.post("/api/review/inbox/x/decision", json={"status": "approved"}).status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(current_user, None)
