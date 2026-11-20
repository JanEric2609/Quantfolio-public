"""Two 500s the authenticated API scan found on 2026-10-15 are 4xx answers now."""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.auth import current_user
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _client():
    from app.main import app

    Session = _memory_db()
    db = Session()
    user = User(username="scan", password_hash="x", role="admin")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: Session()
    app.dependency_overrides[current_user] = lambda: db.get(User, user.id)
    return app, TestClient(app, raise_server_exceptions=False)


def test_a_rule_priority_past_an_integer_column_is_refused():
    app, client = _client()
    try:
        too_big = client.post("/api/budget/rules", json={"pattern": "x", "priority": -239473381312574518622198693888})
        assert too_big.status_code == 422
        assert client.post("/api/budget/rules", json={"pattern": "x", "priority": 5}).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_a_tax_event_with_a_stored_source_ref_is_a_conflict():
    app, client = _client()
    event = {"event_date": "2025-12-31", "event_type": "dividend", "source_ref": "dkb:tx-1"}
    try:
        first = client.post("/api/tax/events", json=event)
        assert first.status_code == 200 and first.json()["estimate"] is True and first.json()["not_tax_advice"] is True
        assert client.post("/api/tax/events", json=event).status_code == 409
    finally:
        app.dependency_overrides.clear()
