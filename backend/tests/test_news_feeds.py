"""The feed manager: list per URL, replace the list (admin), validation."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _client(role: str):
    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.foundation.models.entities import User
    from app.main import create_app

    maker = _memory_db()
    db = maker()
    user = User(username=f"u_{role}", password_hash="x", role=role)
    db.add(user)
    db.commit()
    user_id = user.id
    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user_id)
    return TestClient(app)


def _fake_fetch(url: str, **_kw):
    if "broken" in url:
        return {"ok": False, "items": [], "feed_title": "", "error": "HTTP 500"}
    return {"ok": True, "items": [{"title": "a"}, {"title": "b"}], "feed_title": f"Title of {url[-6:]}"}


def _public(url: str, allow_private: bool = False):
    if "127.0.0.1" in url or "localhost" in url:
        raise ValueError("private address")
    return url, "93.184.216.34"


def test_add_edit_remove_round_trip_through_the_full_list():
    client = _client("admin")
    with patch("app.foundation.rss_provider.fetch_feed", side_effect=_fake_fetch), \
            patch("app.interface.api.news.validate_outbound_url", side_effect=_public):
        assert client.get("/api/news/feeds").json()["source"] == "default"
        body = client.put("/api/news/feeds", json={"urls": ["https://a.example/rss", "https://broken.example/rss",
                                                             "https://a.example/rss"]}).json()
        assert body["source"] == "settings"
        assert [f["url"] for f in body["feeds"]] == ["https://a.example/rss", "https://broken.example/rss"]
        assert body["feeds"][0]["items"] == 2 and body["feeds"][1]["status"] == "error"
        # Remove one: the list is replaced as a whole.
        body = client.put("/api/news/feeds", json={"urls": ["https://a.example/rss"]}).json()
        assert [f["url"] for f in body["feeds"]] == ["https://a.example/rss"]
        # Empty restores the defaults.
        assert client.put("/api/news/feeds", json={"urls": []}).json()["source"] == "default"


def test_private_addresses_and_non_admins_are_refused():
    with patch("app.interface.api.news.validate_outbound_url", side_effect=_public):
        admin = _client("admin")
        assert admin.put("/api/news/feeds", json={"urls": ["http://127.0.0.1/feed"]}).status_code == 422
        user = _client("user")
        assert user.put("/api/news/feeds", json={"urls": ["https://a.example/rss"]}).status_code == 403
