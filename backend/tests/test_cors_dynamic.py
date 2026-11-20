"""CORS origins follow the ``frontend_origin`` setting live (audit D2).

``docs/deployment.md`` says a changed origin applies without a restart, but the
origins were read once at startup. ``DynamicCORSMiddleware`` resolves them per
request (DB -> env -> default) through a short-TTL cache while keeping the
existing CORS behaviour: credentials, methods, headers, preflight.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.foundation.core.db import SessionLocal, create_all, engine
from app.foundation.models.entities import AppSetting
from app.foundation.settings import invalidate_settings_cache, upsert_public_settings
from app.interface import cors as cors_mod
from app.interface.cors import DynamicCORSMiddleware


@pytest.fixture
def client(monkeypatch):
    """The real app on the process-wide in-memory database, frontend_origin unset."""
    from app.main import create_app

    monkeypatch.delenv("FRONTEND_ORIGIN", raising=False)
    create_all()
    _clear_frontend_origin()
    yield TestClient(create_app())  # no `with`: the lifespan is not needed here
    _clear_frontend_origin()


def _clear_frontend_origin() -> None:
    with SessionLocal() as db:
        db.query(AppSetting).filter(AppSetting.key == "frontend_origin").delete()
        db.commit()
    invalidate_settings_cache()


def _set_frontend_origin(value: str) -> None:
    with SessionLocal() as db:
        upsert_public_settings(db, {"frontend_origin": value})


def _acao(response) -> str | None:
    return response.headers.get("access-control-allow-origin")


def test_changing_the_setting_applies_without_a_restart(client):
    _set_frontend_origin("http://one.example")
    assert _acao(client.get("/health", headers={"Origin": "http://one.example"})) == "http://one.example"
    assert _acao(client.get("/health", headers={"Origin": "http://two.example"})) is None

    _set_frontend_origin("http://two.example")

    assert _acao(client.get("/health", headers={"Origin": "http://two.example"})) == "http://two.example"
    assert _acao(client.get("/health", headers={"Origin": "http://one.example"})) is None


def test_csv_setting_allows_every_listed_origin(client):
    _set_frontend_origin("http://a.example, http://b.example")
    for origin in ("http://a.example", "http://b.example"):
        assert _acao(client.get("/health", headers={"Origin": origin})) == origin
    assert _acao(client.get("/health", headers={"Origin": "http://c.example"})) is None


def test_unknown_origin_gets_no_acao_header_on_simple_and_preflight_requests(client):
    _set_frontend_origin("http://known.example")

    simple = client.get("/health", headers={"Origin": "http://evil.example"})
    assert simple.status_code == 200
    assert _acao(simple) is None

    preflight = client.options(
        "/health",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 400
    assert "origin" in preflight.text.lower()
    assert _acao(preflight) is None


def test_credentials_methods_headers_and_preflight_behaviour_are_preserved(client):
    _set_frontend_origin("http://app.example")

    simple = client.get("/health", headers={"Origin": "http://app.example"})
    assert simple.headers["access-control-allow-credentials"] == "true"
    assert "origin" in simple.headers["vary"].lower()

    preflight = client.options(
        "/api/anything",
        headers={
            "Origin": "http://app.example",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "x-custom, content-type",
        },
    )
    assert preflight.status_code == 200
    assert _acao(preflight) == "http://app.example"
    assert preflight.headers["access-control-allow-credentials"] == "true"
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert method in preflight.headers["access-control-allow-methods"]
    assert preflight.headers["access-control-allow-headers"] == "x-custom, content-type"
    assert preflight.headers["access-control-max-age"] == "600"


def test_falls_back_to_the_environment_when_no_setting_is_stored(client, monkeypatch):
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://from-env.example")
    invalidate_settings_cache()
    assert _acao(client.get("/health", headers={"Origin": "http://from-env.example"})) == "http://from-env.example"


def test_creating_the_app_does_not_read_the_database():
    from app.main import create_app

    selects: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _many):
        selects.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        create_app()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert selects == []


# -- unit tests of the middleware with a fake resolver ----------------------


def _wrapped(resolver, *, ttl=5.0, fallback=lambda: ["http://fallback.example"]):
    inner = Starlette(routes=[Route("/", lambda _request: PlainTextResponse("ok"))])
    inner.add_middleware(
        DynamicCORSMiddleware,
        origin_resolver=resolver,
        fallback_origins=fallback,
        ttl_seconds=ttl,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return TestClient(inner)


class _Resolver:
    def __init__(self, origins):
        self.origins = origins
        self.calls = 0
        self.fail = False

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("database unavailable")
        return list(self.origins)


def test_origins_are_cached_for_the_ttl_then_re_resolved(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cors_mod.time, "monotonic", lambda: clock[0])
    resolver = _Resolver(["http://a.example"])
    client = _wrapped(resolver, ttl=5.0)

    for _ in range(4):
        assert _acao(client.get("/", headers={"Origin": "http://a.example"})) == "http://a.example"
    assert resolver.calls == 1

    resolver.origins = ["http://b.example"]
    clock[0] += 4.9
    assert _acao(client.get("/", headers={"Origin": "http://b.example"})) is None  # still cached
    clock[0] += 0.2
    assert _acao(client.get("/", headers={"Origin": "http://b.example"})) == "http://b.example"
    assert resolver.calls == 2


def test_requests_without_an_origin_never_resolve():
    resolver = _Resolver(["http://a.example"])
    client = _wrapped(resolver)
    assert client.get("/").status_code == 200
    assert resolver.calls == 0


def test_resolver_failure_uses_the_fallback_first_then_the_last_known_list(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(cors_mod.time, "monotonic", lambda: clock[0])
    resolver = _Resolver(["http://db.example"])
    resolver.fail = True
    client = _wrapped(resolver, ttl=1.0, fallback=lambda: ["http://fallback.example"])

    # Never resolved yet: the environment default applies, nothing crashes.
    assert _acao(client.get("/", headers={"Origin": "http://fallback.example"})) == "http://fallback.example"

    resolver.fail = False
    clock[0] += 2
    assert _acao(client.get("/", headers={"Origin": "http://db.example"})) == "http://db.example"

    resolver.fail = True
    clock[0] += 2
    assert _acao(client.get("/", headers={"Origin": "http://db.example"})) == "http://db.example"
    assert _acao(client.get("/", headers={"Origin": "http://fallback.example"})) is None


def test_a_wildcard_entry_keeps_meaning_any_origin():
    client = _wrapped(_Resolver(["*"]))
    response = client.get("/", headers={"Origin": "http://anything.example"})
    assert _acao(response) == "http://anything.example"
    assert response.headers["access-control-allow-credentials"] == "true"
