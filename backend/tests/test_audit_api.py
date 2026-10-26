"""GET /api/audit returns the stored events (the scan found it failing once a row existed)."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.auth import current_user
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import AuditLog, User
from app.main import app


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_audit_events_list_with_their_time_as_a_string():
    db = _memory_db()
    user = User(username="owner", password_hash="x")
    db.add(user)
    db.commit()
    db.add(AuditLog(user_id=user.id, event_type="settings.update", source="api", message="changed"))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/audit")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    [event] = response.json()
    assert event["event_type"] == "settings.update"
    assert isinstance(event["created_at"], str) and event["created_at"][:4].isdigit()
