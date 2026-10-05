"""Cross-site request guard, production docs toggle, host allow-list, /healthz, PUT /api/settings."""
import logging

import httpx
import pytest
from conftest import _memory_db
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.foundation.core.config import Settings
from app.foundation.core.db import get_db
from app.foundation.core.security import limiter
from app.foundation.schemas import SettingsPayload
from app.interface.middleware import CrossSiteRequestGuard
from app.main import _allowed_hosts, _docs_enabled, app, create_app


@pytest.fixture(autouse=True)
def _clean():
    limiter.reset()
    yield
    app.dependency_overrides.clear()
    limiter.reset()


def _guarded(allowed=("https://qf.example.ts.net",)) -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/x", ok, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])])
    return TestClient(CrossSiteRequestGuard(inner, allowed_origins=lambda: list(allowed)))


# -- CrossSiteRequestGuard ---------------------------------------------------


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_unsafe_methods_marked_cross_site_are_refused(method):
    client = _guarded()

    response = getattr(client, method)("/x", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
    assert response.json() == {"error": {"code": 403, "message": "Cross-site request blocked"}}


def test_foreign_origin_is_refused():
    client = _guarded()

    assert client.post("/x", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/x", headers={"Origin": "null"}).status_code == 403
    # Sec-Fetch-Site same-site but an origin nobody allowed (a sibling subdomain page).
    assert client.post(
        "/x", headers={"Sec-Fetch-Site": "same-site", "Origin": "https://other.example.ts.net"}
    ).status_code == 403


def test_allowed_frontend_origin_passes():
    client = _guarded()

    assert client.post("/x", headers={"Origin": "https://qf.example.ts.net"}).status_code == 200
    assert client.post("/x", headers={"Origin": "https://QF.example.ts.net/"}).status_code == 200


def test_same_origin_as_the_host_header_passes_even_if_not_listed():
    """A stale FRONTEND_ORIGIN must not break the SPA served from the very host it calls."""
    client = _guarded(allowed=())

    response = client.post("/x", headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})

    assert response.status_code == 200


def test_requests_without_browser_headers_pass():
    """curl, the test client and Telegram's webhook send neither header."""
    client = _guarded()

    assert client.post("/x").status_code == 200
    assert client.post("/x", headers={"Sec-Fetch-Site": "none"}).status_code == 200
    assert client.post("/x", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_safe_methods_are_never_blocked():
    client = _guarded()

    assert client.get("/x", headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}).status_code == 200
    assert client.options("/x", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_origin_lookup_failure_fails_closed_for_foreign_origins():
    def broken():
        raise RuntimeError("db down")

    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/x", ok, methods=["POST"])])
    client = TestClient(CrossSiteRequestGuard(inner, allowed_origins=broken))

    assert client.post("/x", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/x").status_code == 200


def test_the_real_app_blocks_cross_site_logout_but_not_plain_requests():
    db = _memory_db()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    blocked = client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
    foreign = client.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    plain = client.post("/api/auth/logout")
    own_origin = client.post("/api/auth/logout", headers={"Origin": "http://localhost:5173"})

    assert blocked.status_code == foreign.status_code == 403
    assert plain.status_code == own_origin.status_code == 200


def test_telegram_webhook_with_its_secret_header_is_not_blocked_as_cross_site():
    db = _memory_db()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    response = client.post("/api/telegram/webhook", json={})

    # Reaches the handler (which answers 403 for the missing secret, not for cross-site).
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Invalid webhook secret"


# -- docs / schema off in production -----------------------------------------


def test_docs_are_on_outside_production_and_off_in_production():
    assert _docs_enabled(Settings(app_env="local"))
    assert _docs_enabled(Settings(app_env="development"))
    assert not _docs_enabled(Settings(app_env="production"))
    assert not _docs_enabled(Settings(app_env=" Production "))


def test_production_app_serves_no_docs_or_schema(monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(app_env="production"))
    prod = create_app()
    client = TestClient(prod)

    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    # The exporter script builds the schema in-process and keeps working.
    assert "/api/auth/login" in prod.openapi()["paths"]


def test_default_app_still_serves_docs():
    client = TestClient(app)

    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


# -- ALLOWED_HOSTS -----------------------------------------------------------


def test_allowed_hosts_parses_the_csv_and_is_off_when_unset():
    assert _allowed_hosts(Settings(allowed_hosts=None)) == []
    assert _allowed_hosts(Settings(allowed_hosts="")) == []
    assert _allowed_hosts(Settings(allowed_hosts=" 10.0.0.23 , quantfolio.local,,")) == [
        "10.0.0.23",
        "quantfolio.local",
    ]


def test_trusted_host_middleware_rejects_unknown_hosts_when_configured(monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(allowed_hosts="quantfolio.local,testserver"))
    configured = TestClient(create_app())

    assert configured.get("/health").status_code == 200
    assert configured.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_any_host_is_accepted_when_unset():
    client = TestClient(app)

    assert client.get("/health", headers={"Host": "anything.example"}).status_code == 200


# -- /healthz ----------------------------------------------------------------


def test_healthz_makes_no_outbound_llm_probe(monkeypatch):
    def forbidden(*_a, **_kw):
        raise AssertionError("/healthz must not make an outbound HTTP request")

    # The TestClient is itself an httpx.Client (over an in-process transport), so
    # guard the real network paths: async sends and the real sync transport.
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    db = _memory_db()
    app.dependency_overrides[get_db] = lambda: db

    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert "db_up" in body["checks"]
    assert not [key for key in body["checks"] if key.startswith("llm_")]


def test_healthz_stays_unauthenticated():
    db = _memory_db()
    app.dependency_overrides[get_db] = lambda: db

    assert TestClient(app).get("/healthz").status_code == 200


def test_llm_backends_are_probed_by_an_authenticated_endpoint():
    db = _memory_db()
    app.dependency_overrides[get_db] = lambda: db

    assert TestClient(app).get("/api/finagent/llm/health").status_code == 401


# -- PUT /api/settings -------------------------------------------------------


def _put(db, settings=None, integrations=None):
    from app.interface.api.settings import update_settings

    return update_settings(SettingsPayload(settings=settings or {}, integrations=integrations or {}), db, None)


@pytest.mark.parametrize(
    "key",
    ["worker_heartbeat_json", "connection_tests_json", "regime_snapshot_json", "risk_free_rate_pct", "telegram_update_offset", "made_up"],
)
def test_put_settings_rejects_keys_outside_the_documented_set(key):
    db = _memory_db()

    with pytest.raises(HTTPException) as exc:
        _put(db, {key: "x", "currency": "EUR"})

    assert exc.value.status_code == 400
    assert exc.value.detail == {"errors": {key: "unknown setting"}}
    from app.foundation.settings import get_setting_row

    assert get_setting_row(db, key) is None
    assert get_setting_row(db, "currency") is None  # nothing was half-applied


def test_put_settings_still_accepts_documented_keys_the_dkb_username_and_secret_names():
    from app.foundation.settings import get_public_settings, set_secret

    db = _memory_db()
    set_secret(db, "dkb", "1234", {"username": "old"})

    _put(db, {"monthly_contribution_eur": 500, "dkb_username": "jan", "finnhub": "ignored-here"})

    assert get_public_settings(db)["monthly_contribution_eur"] == 500
    assert "finnhub" not in get_public_settings(db)
    assert "dkb_username" not in get_public_settings(db)


def test_put_settings_rejects_unknown_integrations():
    db = _memory_db()

    with pytest.raises(HTTPException) as exc:
        _put(db, integrations={"not-a-service": {"secret": "x"}})

    assert exc.value.status_code == 400


def test_put_settings_validates_the_telegram_mode():
    db = _memory_db()

    with pytest.raises(HTTPException) as exc:
        _put(db, {"telegram_mode": "carrier-pigeon"})

    assert exc.value.status_code == 422


def test_internal_state_is_still_written_by_the_code_that_owns_it():
    from app.foundation.settings import get_setting_row, record_connection_test, upsert_public_settings

    db = _memory_db()
    record_connection_test(db, "finnhub", True, "ok")
    upsert_public_settings(db, {"telegram_update_offset": 42})

    assert get_setting_row(db, "connection_tests_json")["finnhub"]["ok"] is True
    assert get_setting_row(db, "telegram_update_offset") == 42


# -- AI errors ---------------------------------------------------------------


def test_llm_recommendation_error_keeps_the_cause_server_side(caplog):
    from app.interface.api.ai import _llm_recommendation_error

    secret_cause = RuntimeError("connect to http://10.0.0.22:8080 failed: key sk-live-123")
    with caplog.at_level(logging.WARNING, logger="app.interface.api.ai"):
        error = _llm_recommendation_error(secret_cause)

    assert error.status_code == 503
    assert "10.0.0.22" not in str(error.detail)
    assert "sk-live-123" not in str(error.detail)
    assert "debug" not in error.detail
    assert "10.0.0.22" in caplog.text
