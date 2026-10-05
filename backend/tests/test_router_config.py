"""Tests for RouterConfig with resolver-based settings."""
from conftest import _memory_db


def test_routerconfig_uses_resolver():
    """Test that RouterConfig uses resolver functions for llm_base_url and llm_model."""
    from app.foundation.llm.router import RouterConfig
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    upsert_public_settings(
        db, {"llm_base_url": "http://h:8080", "llm_model": "m1"}
    )

    config = RouterConfig(db)

    assert config.llm_base_url == "http://h:8080/v1"
    assert config.llm_model == "m1"


def test_routerconfig_env_fallback(monkeypatch):
    """Test that RouterConfig falls back to env var when no DB row exists."""
    from app.foundation.llm.router import RouterConfig

    db = _memory_db()
    # Fresh in-memory db, no llm_base_url row
    monkeypatch.setenv("LLM_LOCAL_URL", "http://envhost:8080")

    config = RouterConfig(db)

    assert config.llm_base_url == "http://envhost:8080/v1"
