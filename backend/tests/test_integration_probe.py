import importlib.util
import types
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.foundation.core.db import Base
from app.foundation.schemas import IntegrationTestRequest
from app.foundation.settings import set_secret

_SETTINGS_PATH = Path(__file__).resolve().parents[1] / "app" / "interface" / "api" / "settings.py"
_SETTINGS_SPEC = importlib.util.spec_from_file_location("settings_api_for_tests", _SETTINGS_PATH)
assert _SETTINGS_SPEC is not None
settings_api = importlib.util.module_from_spec(_SETTINGS_SPEC)
assert _SETTINGS_SPEC.loader is not None
_SETTINGS_SPEC.loader.exec_module(settings_api)
run_integration_probe = settings_api.test_integration


def _db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    return sessionmaker(bind=e, autoflush=False, autocommit=False)()


def test_llm_probe_ok(monkeypatch):
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: types.SimpleNamespace(status_code=200))
    r = run_integration_probe(IntegrationTestRequest(service="llm", meta={"base_url": "http://127.0.0.1:8080/v1"}), _db(), None)
    assert r.ok is True and "each" not in r.message.lower()  # message should indicate reachable


def test_llm_probe_down(monkeypatch):
    def boom(self, url, **kw): raise httpx.ConnectError("boom")
    monkeypatch.setattr(httpx.Client, "get", boom)
    r = run_integration_probe(IntegrationTestRequest(service="llm", meta={"base_url": "http://127.0.0.1:8080/v1"}), _db(), None)
    assert r.ok is False


def test_dkb_shape_no_creds():
    """No saved credentials — lightweight credential check returns ok=False."""
    r = run_integration_probe(IntegrationTestRequest(service="dkb", value="pin", meta={"username": "u", "product_id": "p"}), _db(), None)
    assert r.ok is False
    assert "credentials not saved" in r.message.lower()


def test_market_provider_uses_submitted_key(monkeypatch):
    seen = {}

    class FakeProv:
        def __init__(self, api_key):
            seen["api_key"] = api_key

        def get_quote(self, sym): return {"ok": True, "error": None, "quality": {"warnings": []}}

    monkeypatch.setitem(settings_api.QUOTE_PROVIDERS, "alphavantage", FakeProv)
    r = run_integration_probe(IntegrationTestRequest(service="alphavantage", value="k"), _db(), None)
    assert r.ok is True
    assert seen["api_key"] == "k"


def test_market_provider_falls_back_to_saved_key(monkeypatch):
    seen = {}
    db = _db()
    set_secret(db, "finnhub", "saved-k")

    class FakeProv:
        def __init__(self, api_key):
            seen["api_key"] = api_key

        def get_quote(self, sym): return {"ok": True, "error": None, "quality": {"warnings": []}}

    monkeypatch.setitem(settings_api.QUOTE_PROVIDERS, "finnhub", FakeProv)
    r = run_integration_probe(IntegrationTestRequest(service="finnhub", value=None), db, None)
    assert r.ok is True
    assert seen["api_key"] == "saved-k"


def test_service_literal_accepts_new():
    from app.foundation.schemas import IntegrationTestRequest
    for s in ("fred", "eod", "anthropic", "openai"):
        IntegrationTestRequest(service=s)  # must not raise


def test_read_settings_shows_resolved_llm(monkeypatch):
    monkeypatch.setenv("LLM_LOCAL_URL", "http://envllm:8080")
    payload = settings_api.read_settings(_db(), None)
    assert payload.settings["llm_base_url"] == "http://envllm:8080/v1"
