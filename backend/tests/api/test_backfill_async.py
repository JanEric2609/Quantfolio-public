"""API tests for async price-backfill endpoints (proposal P4)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user


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
def client(db, monkeypatch):
    from app.main import app

    # Don't actually spin up the scheduler in tests. Phase E: backfill_jobs
    # imports submit_job lazily from the generic infra, so patch it there.
    monkeypatch.setattr("app.foundation.jobs.submit_job", lambda *a, **k: "sched-x")

    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    yield TestClient(app, raise_server_exceptions=False), user
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(current_user, None)


def test_enqueue_then_poll_status(client):
    tc, _user = client

    resp = tc.post("/api/data/backfill/holdings/async?days=900")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    poll = tc.get(f"/api/data/backfill/{job_id}")
    assert poll.status_code == 200
    status = poll.json()
    assert status["job_id"] == job_id
    assert status["status"] == "queued"
    assert status["days"] == 900


def test_poll_unknown_job_is_404(client):
    tc, _user = client
    resp = tc.get("/api/data/backfill/does-not-exist")
    assert resp.status_code == 404
