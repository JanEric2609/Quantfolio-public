from conftest import _memory_db

from app.foundation.settings import (
    DEFAULT_PUBLIC_SETTINGS,
    FALLBACK_RISK_FREE_RATE,
    get_risk_free_rate,
    normalize_base_url,
    probe_root,
    resolve_frontend_origins,
    resolve_setting,
    upsert_public_settings,
)


def test_resolve_prefers_db_row(monkeypatch):
    """After upserting a DB row, resolve_setting should prefer it over env/default."""
    db = _memory_db()
    # Ensure env var is not set
    monkeypatch.delenv("LLM_LOCAL_URL", raising=False)

    upsert_public_settings(db, {"llm_base_url": "http://db:1/v1"})
    result = resolve_setting(db, "llm_base_url", "LLM_LOCAL_URL", "http://default:9/v1")
    assert result == "http://db:1/v1"


def test_resolve_falls_back_to_env(monkeypatch):
    """With no DB row, resolve_setting should fall back to env var."""
    db = _memory_db()
    monkeypatch.setenv("LLM_LOCAL_URL", "http://env:2")

    result = resolve_setting(db, "llm_base_url", "LLM_LOCAL_URL", "http://default:9/v1")
    assert result == "http://env:2"


def test_resolve_falls_back_to_default(monkeypatch):
    """With no DB row and no env var, resolve_setting should return default."""
    db = _memory_db()
    monkeypatch.delenv("LLM_LOCAL_URL", raising=False)

    result = resolve_setting(db, "llm_base_url", "LLM_LOCAL_URL", "http://default:9/v1")
    assert result == "http://default:9/v1"


def test_normalize_base_url_adds_v1():
    """normalize_base_url should strip trailing / and add /v1 if not present."""
    assert normalize_base_url("http://h:8080") == "http://h:8080/v1"
    assert normalize_base_url("http://h:8080/") == "http://h:8080/v1"
    assert normalize_base_url("http://h:8080/v1") == "http://h:8080/v1"


def test_probe_root_strips_v1():
    """probe_root should strip trailing / and /v1 suffix."""
    assert probe_root("http://h:8080/v1") == "http://h:8080"
    assert probe_root("http://h:8080/") == "http://h:8080"


def test_resolve_frontend_origins_splits_csv(monkeypatch):
    """resolve_frontend_origins should split CSV and strip whitespace."""
    db = _memory_db()
    monkeypatch.delenv("FRONTEND_ORIGIN", raising=False)

    upsert_public_settings(db, {"frontend_origin": "http://a, http://b"})
    result = resolve_frontend_origins(db)
    assert result == ["http://a", "http://b"]


def test_default_llm_model_is_qwen35_9b():
    """DEFAULT_PUBLIC_SETTINGS['llm_model'] should be 'qwen3.5-9b'."""
    assert DEFAULT_PUBLIC_SETTINGS["llm_model"] == "qwen3.5-9b"


def test_default_llm_base_url_not_8001():
    """DEFAULT_PUBLIC_SETTINGS['llm_base_url'] should not contain '8001'."""
    assert "8001" not in DEFAULT_PUBLIC_SETTINGS["llm_base_url"]


def test_get_risk_free_rate_falls_back_to_hardcoded_nonzero_default():
    """With no cached ECB fetch, must return the hardcoded fallback — never 0.0
    (F12/F13: a silent 0% risk-free rate overstates every risk-adjusted metric)."""
    db = _memory_db()
    assert get_risk_free_rate(db) == FALLBACK_RISK_FREE_RATE
    assert FALLBACK_RISK_FREE_RATE != 0.0


def test_get_risk_free_rate_prefers_db_cache():
    db = _memory_db()
    upsert_public_settings(db, {"risk_free_rate_pct": 0.0345})
    assert get_risk_free_rate(db) == 0.0345
