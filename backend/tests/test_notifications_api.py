"""Router tests for /api/notifications (audit-fixes-2026-08 todo 7)."""
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


def _client_for(db, user):
    from app.main import app
    from app.foundation.core.db import get_db
    from app.foundation.auth import current_user

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def test_list_returns_rows_and_unread_count():
    from app.foundation.models.entities import Notification, User

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    other = User(username="eve", password_hash="hash")
    db.add_all([user, other])
    db.commit()
    for i in range(3):
        db.add(Notification(user_id=user.id, source="nudge", title=f"Note {i}"))
    # Another user's row must not leak into the list.
    db.add(Notification(user_id=other.id, source="nudge", title="Not mine"))
    db.commit()

    client = _client_for(db, user)
    try:
        response = client.get("/api/notifications")
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["items"]) == 3
        assert payload["unread_count"] == 3
        titles = [row["title"] for row in payload["items"]]
        assert set(titles) == {"Note 0", "Note 1", "Note 2"}
    finally:
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides.clear()


def test_read_all_clears_unread_count():
    from app.foundation.models.entities import Notification, User

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    for i in range(3):
        db.add(Notification(user_id=user.id, source="dkb", title=f"Sync {i}", severity="warning"))
    db.commit()

    client = _client_for(db, user)
    try:
        mark = client.post("/api/notifications/read-all")
        assert mark.status_code == 200

        listing = client.get("/api/notifications").json()
        assert listing["unread_count"] == 0
        assert len(listing["items"]) == 3
        assert all(row["read_at"] is not None for row in listing["items"])
    finally:
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides.clear()


def test_mark_single_read_and_unknown_id_404():
    from app.foundation.models.entities import Notification, User

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    note = Notification(user_id=user.id, source="system", title="Hello")
    db.add(note)
    db.commit()

    client = _client_for(db, user)
    try:
        read = client.post(f"/api/notifications/{note.id}/read")
        assert read.status_code == 200
        assert read.json()["read_at"] is not None

        missing = client.post("/api/notifications/does-not-exist/read")
        assert missing.status_code == 404
        body = missing.json()
        # App-wide error envelope (main.py StarletteHTTPException handler).
        assert body["error"]["code"] == 404
        assert "message" in body["error"]
    finally:
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides.clear()


def test_latest_50_cap_newest_first():
    from datetime import UTC, datetime, timedelta

    from app.foundation.models.entities import Notification, User

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    base = datetime.now(UTC) - timedelta(hours=100)
    for i in range(60):
        db.add(
            Notification(
                user_id=user.id,
                source="system",
                title=f"n{i}",
                created_at=base + timedelta(minutes=i),
            )
        )
    db.commit()

    client = _client_for(db, user)
    try:
        payload = client.get("/api/notifications").json()
        assert len(payload["items"]) == 50
        assert payload["items"][0]["title"] == "n59"
        assert payload["unread_count"] == 60
    finally:
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides.clear()


def test_malformed_inputs_tolerated_gracefully():
    """Negative/absurd limit clamps instead of erroring; unknown severity passes through."""
    from app.foundation.models.entities import Notification, User

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.add(Notification(user_id=user.id, source="news", title="odd", severity="banana"))
    db.commit()

    client = _client_for(db, user)
    try:
        assert client.get("/api/notifications?limit=-5").status_code == 200
        assert client.get("/api/notifications?limit=999999").status_code == 200
        payload = client.get("/api/notifications").json()
        assert payload["items"][0]["severity"] == "banana"
    finally:
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides.clear()


def test_cannot_mark_another_users_notification():
    from app.foundation.models.entities import Notification, User

    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    other = User(username="eve", password_hash="hash")
    db.add_all([user, other])
    db.commit()
    foreign = Notification(user_id=other.id, source="system", title="Eve only")
    db.add(foreign)
    db.commit()

    client = _client_for(db, user)
    try:
        response = client.post(f"/api/notifications/{foreign.id}/read")
        assert response.status_code == 404
    finally:
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides.clear()
