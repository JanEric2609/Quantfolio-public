"""justETF provider: a network failure is a failed result, never an exception.

The API security scan (no network) hit ``POST /api/market/providers/justetf/test``
and got a 500: ``load_live_quote`` raised a DNS error straight through
``_call_with_timeout``. A successful quote also lacked ``ok``, so the provider
test and the registry chain read it as a failure.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

justetf_scraping = pytest.importorskip("justetf_scraping")

from app.foundation.providers.justetf_provider import JustETFProvider  # noqa: E402


def test_quote_network_error_returns_failed_result(monkeypatch):
    def _boom(_symbol):
        raise OSError("[Errno -3] Temporary failure in name resolution")

    monkeypatch.setattr(justetf_scraping, "load_live_quote", _boom)
    result = JustETFProvider().get_quote("IE00B4L5Y983")
    assert result["ok"] is False
    assert "name resolution" in result["error"]


def test_history_network_error_returns_failed_result(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr(justetf_scraping, "load_chart", _boom)
    result = JustETFProvider().get_history("IE00B4L5Y983", days=30)
    assert result["ok"] is False
    assert "offline" in result["error"]


def test_successful_quote_is_ok_with_data(monkeypatch):
    quote = SimpleNamespace(last=101.5, bid=101.4, ask=101.6, currency="EUR", exchange="gettex", timestamp=None)
    monkeypatch.setattr(justetf_scraping, "load_live_quote", lambda _s: quote)
    result = JustETFProvider().get_quote("IE00B4L5Y983")
    assert result["ok"] is True
    assert result["provider"] == "justetf"
    assert result["data"]["price"] == 101.5


def test_provider_test_endpoint_answers_200_when_justetf_is_unreachable(monkeypatch):
    """The scan's case: POST /api/market/providers/justetf/test with no network."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.foundation.auth import current_user
    from app.foundation.core.db import Base, get_db
    from app.foundation.models.entities import User
    from app.main import app

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    user = User(username="owner", password_hash="x")
    db.add(user)
    db.commit()

    def _boom(_symbol):
        raise OSError("[Errno -3] Temporary failure in name resolution")

    monkeypatch.setattr(justetf_scraping, "load_live_quote", _boom)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        response = TestClient(app).post("/api/market/providers/justetf/test")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False
    assert "name resolution" in body["message"]
