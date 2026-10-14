"""API security scan: auth coverage and an authenticated fuzz pass.

Replaces the APIsec starter workflow, which scanned APIsec's demo project and
could not reach this API (it is never exposed to the internet).

1. Auth coverage (default suite): every route needs ``current_user`` in its
   dependency tree unless it is on ``PUBLIC_ROUTES``. Auth is attached per
   handler, so one forgotten ``Depends`` serves data to anyone.
2. Session tokens (default suite): a missing, forged, expired or revoked
   cookie is refused on a protected route.
3. Authenticated fuzz (``SCHEMATHESIS=1``, CI job ``api-security-scan``):
   schemathesis drives every operation with a valid admin session, so the
   generated input reaches the handlers instead of stopping at 401. It runs
   in a network namespace with no route out; no generated input may produce
   a 5xx.

Run the fuzz locally:

    cd backend && DATABASE_URL="sqlite:///:memory:" SCHEMATHESIS=1 \
        unshare -rn .venv/bin/pytest tests/test_api_security.py -q -n 6

Set ``API_SCAN_ONLY`` ("METHOD /path;METHOD /path") to re-run some operations.
"""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import auth
from app.foundation.core.config import get_settings
from app.foundation.core import db as db_module
from app.foundation.core.db import Base, get_db
from app.foundation.core.security import create_token, limiter
from app.foundation.models.entities import User
from app.main import app
from conftest import _create_ormless_tables

#: (method, path) -> why it is reachable without a session.
PUBLIC_ROUTES = {
    ("GET", "/health"): "liveness probe",
    ("GET", "/health/ready"): "readiness probe",
    ("GET", "/healthz"): "liveness probe",
    ("GET", "/api/setup/status"): "first-run screen: does the instance need a setup token",
    ("GET", "/api/auth/status"): "login screen: is there a user yet",
    ("POST", "/api/auth/register"): "first user only, needs the setup token",
    ("POST", "/api/auth/login"): "login, rate-limited",
    ("POST", "/api/auth/password-reset/request"): "rate-limited, answers the same for any username",
    ("POST", "/api/auth/password-reset"): "needs the reset token",
    ("POST", "/api/auth/logout"): "clears the cookie",
    ("POST", "/api/auth/passkey/authenticate/options"): "passkey login, step 1",
    ("POST", "/api/auth/passkey/authenticate/verify"): "passkey login, step 2 (signed challenge)",
    ("POST", "/api/auth/passkey/authenticate"): "passkey login, legacy single step",
    ("POST", "/api/telegram/webhook"): "Telegram calls it; checks X-Telegram-Bot-Api-Secret-Token",
}

_GUARDS = {auth.current_user, auth.require_admin}


def _api_routes() -> list[tuple[set[str], str, object]]:
    """(methods, path, dependant) of every APIRoute in the app.

    FastAPI 0.143 stopped copying an included router's routes into
    ``app.routes`` (it keeps one nested entry per router), so walk them with
    ``iter_route_contexts``; older versions have no such helper and are flat.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        return [(r.methods, r.path, r.dependant) for r in app.routes if isinstance(r, APIRoute)]
    return [
        (c.methods, c.path, c.dependant)
        for c in iter_route_contexts(app.routes)
        if isinstance(c.original_route, APIRoute)
    ]


def _guarded(dependant) -> bool:
    return dependant.call in _GUARDS or any(_guarded(d) for d in dependant.dependencies)


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    _create_ormless_tables(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _admin(db) -> User:
    user = db.query(User).filter_by(username="scan-admin").one_or_none()
    if user is None:
        user = User(username="scan-admin", password_hash="!", role="admin")
        db.add(user)
        db.commit()
    return user


def _cookie(user: User) -> str:
    return create_token(user.id, {"username": user.username, "session_version": user.session_version or 0})


# ── 1. Auth coverage ────────────────────────────────────────────────────


def test_every_route_needs_a_session_unless_listed_as_public():
    routes = _api_routes()
    assert len(routes) > 300, f"only {len(routes)} routes found; the route walk is broken"
    open_routes = {(method, path) for methods, path, dependant in routes if not _guarded(dependant) for method in methods}
    unexpected = open_routes - set(PUBLIC_ROUTES)
    assert not unexpected, f"routes without Depends(current_user): {sorted(unexpected)}"
    stale = set(PUBLIC_ROUTES) - open_routes
    assert not stale, f"PUBLIC_ROUTES lists routes that are gone or now guarded: {sorted(stale)}"


# ── 2. Session tokens ───────────────────────────────────────────────────


@pytest.fixture
def client():
    db = _db()
    app.dependency_overrides[get_db] = lambda: db
    limiter.reset()
    try:
        yield TestClient(app), db
    finally:
        app.dependency_overrides.clear()
        db.close()


def _me(c: TestClient, token: str | None) -> int:
    c.cookies.clear()
    if token is not None:
        c.cookies.set(auth.COOKIE_NAME, token)
    return c.get("/api/auth/me").status_code


def test_a_valid_session_is_accepted(client):
    c, db = client
    assert _me(c, _cookie(_admin(db))) == 200


def test_missing_forged_expired_and_revoked_sessions_are_refused(client):
    c, db = client
    user = _admin(db)
    now = datetime.now(UTC)
    claims = {"sub": user.id, "username": user.username, "session_version": 0}
    forged = pyjwt.encode({**claims, "exp": now + timedelta(hours=1)}, "not-the-secret", algorithm="HS256")
    expired = pyjwt.encode(
        {**claims, "iat": int((now - timedelta(hours=2)).timestamp()), "exp": now - timedelta(hours=1)},
        get_settings().jwt_secret, algorithm="HS256",
    )
    unsigned = pyjwt.encode({**claims, "exp": now + timedelta(hours=1)}, None, algorithm="none")
    unknown_user = create_token("no-such-user", {"session_version": 0})
    assert _me(c, None) == 401
    assert _me(c, "garbage") == 401
    assert _me(c, forged) == 401
    assert _me(c, expired) == 401
    assert _me(c, unsigned) == 401
    assert _me(c, unknown_user) == 401

    revoked = _cookie(user)
    user.session_version = (user.session_version or 0) + 1
    db.commit()
    assert _me(c, revoked) == 401


def test_admin_routes_refuse_a_plain_user(client):
    c, db = client
    user = User(username="plain", password_hash="!", role="user")
    db.add(user)
    db.commit()
    admin_routes = [
        (sorted(methods)[0], path) for methods, path, dependant in _api_routes()
        if _requires_admin(dependant) and "{" not in path
    ]
    assert admin_routes, "expected at least one admin-only route"
    c.cookies.set(auth.COOKIE_NAME, _cookie(user))
    for method, path in admin_routes:
        assert c.request(method, path).status_code == 403, (method, path)


def _requires_admin(dependant) -> bool:
    return dependant.call is auth.require_admin or any(_requires_admin(d) for d in dependant.dependencies)


def test_a_decimal_bound_violation_is_a_422_not_a_500(client):
    # Found by the authenticated scan: the error context carries
    # Decimal("0"), which the 422 handler could not serialise.
    c, db = client
    c.cookies.set(auth.COOKIE_NAME, _cookie(_admin(db)))
    response = c.put("/api/dkb/positions/x/cost", json={"einstandswert_eur": 0})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["ctx"] == {"gt": 0}


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/api/budget/summary?year=371870", None),
    ("GET", "/api/budget/summary?year=2026&month=13", None),
    ("GET", "/api/budget/envelopes/history?months=0", None),
    ("GET", "/api/budget/envelopes/history?months=5000", None),
    ("GET", "/api/tax/overview?year=-1", None),
    ("GET", "/api/market/history/SPY?days=10000000", None),
    ("GET", "/api/quant/experiments?limit=-1", None),
    ("GET", "/api/performance/ledger?composite_id=x&offset=-1", None),
    # A Query(...) default used to drop the validator of an annotated type.
    ("GET", "/api/research/stock/SPY/sentiment?days=2814030", None),
    ("GET", "/api/news/macro?offset=25149468742627696640", None),
    ("POST", "/api/tax/vorabpauschale/estimate", {
        "isin": "IE00B4L5Y983", "tax_year": 2025, "fund_value_start_of_year_eur": 4.4e27,
        "fund_value_end_of_year_eur": 0.0,
    }),
    ("POST", "/api/finagent/budget/query", {"query": "q", "year": 2026, "month": 0}),
])
def test_out_of_range_input_is_a_422(client, method, path, body):
    # Each of these answered 500 before (found by the authenticated scan).
    c, db = client
    c.cookies.set(auth.COOKIE_NAME, _cookie(_admin(db)))
    response = c.request(method, path, json=body)
    assert response.status_code == 422, response.text


def test_the_bounds_stay_out_of_the_openapi_contract():
    # oasdiff counts a new schema bound as breaking; the bounds are validators.
    spec = app.openapi()
    summary = {p["name"]: p["schema"] for p in spec["paths"]["/api/budget/summary"]["get"]["parameters"]}
    assert "minimum" not in str(summary["year"]) and "maximum" not in str(summary["year"])


def test_a_composite_takes_only_the_callers_own_portfolios_once(client):
    from app.foundation.models.entities import CompositeMembership, Portfolio

    c, db = client
    me = _admin(db)
    other = User(username="other", password_hash="!")
    db.add(other)
    db.flush()
    db.add_all([Portfolio(id="mine", user_id=me.id, name="Mine"), Portfolio(id="theirs", user_id=other.id, name="Theirs")])
    db.commit()
    c.cookies.set(auth.COOKIE_NAME, _cookie(me))

    duplicated = c.post("/api/performance/composites", json={"name": "x", "portfolio_ids": ["mine", "mine"]})
    assert duplicated.status_code == 200, duplicated.text
    assert duplicated.json()["research"]["portfolio_ids"] == ["mine"]
    assert c.post("/api/performance/composites", json={"name": "y", "portfolio_ids": ["theirs"]}).status_code == 404
    assert c.post("/api/performance/composites", json={"name": "z", "portfolio_ids": ["nope"]}).status_code == 404
    assert {m.portfolio_id for m in db.query(CompositeMembership).all()} == {"mine"}


def test_a_stored_mc_run_can_be_read_back(client):
    # Found by the scan: both reads answered 500 once a run existed.
    c, db = client
    c.cookies.set(auth.COOKIE_NAME, _cookie(_admin(db)))
    run = c.post("/api/quant/mc/run", json={
        "model_params": {"S0": 100.0, "mu": 0.05, "sigma": 0.2}, "payoff_params": {"strike": 100.0},
        "n_paths": 200, "horizon_steps": 10, "seed": 1,
    })
    assert run.status_code == 200, run.text
    run_id = run.json()["ledger_ref"]
    listed = c.get("/api/quant/mc/runs")
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()] == [run_id]
    one = c.get(f"/api/quant/mc/run/{run_id}")
    assert one.status_code == 200 and one.json()["spec"]["n_paths"] == 200


def test_an_unreachable_llm_is_a_503_not_a_500(client, monkeypatch):
    # Found by the scan: a refused connection to llama.cpp answered 500.
    from app.foundation.llm.base import LlmUnavailableError
    from app.foundation.llm.local_llama import LocalLlamaBackend

    async def refused(self, texts):
        raise LlmUnavailableError("Local embedding endpoint unreachable: Connection error.")

    monkeypatch.setattr(LocalLlamaBackend, "embed", refused)
    c, db = client
    c.cookies.set(auth.COOKIE_NAME, _cookie(_admin(db)))
    response = c.post("/api/finagent/embed/index", json=["a note"])
    assert response.status_code == 503
    assert response.json()["error"]["reason"] == "llm_unavailable"


def test_the_llm_backends_raise_unavailable_on_a_refused_connection():
    import asyncio

    import httpx
    from openai import APIConnectionError

    from app.foundation.llm.base import LlmUnavailableError
    from app.foundation.llm.local_llama import LocalLlamaBackend

    backend = LocalLlamaBackend("http://127.0.0.1:9/v1", "m")

    class Refusing:
        class chat:
            class completions:
                @staticmethod
                async def create(**_):
                    raise APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:9/v1"))

        class embeddings:
            @staticmethod
            async def create(**_):
                raise APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:9/v1"))

    backend._get_client = lambda: Refusing  # type: ignore[method-assign]
    with pytest.raises(LlmUnavailableError):
        asyncio.run(backend.complete([{"role": "user", "content": "hi"}]))
    with pytest.raises(LlmUnavailableError):
        asyncio.run(backend.embed(["hi"]))


# ── 3. Authenticated fuzz ───────────────────────────────────────────────

if os.environ.get("SCHEMATHESIS") == "1":
    import schemathesis
    from hypothesis import HealthCheck, settings

    schema = schemathesis.openapi.from_asgi("/openapi.json", app)

    # "unsupported method" and "status code conformance" are left out for the
    # reasons in test_api_contract_schemathesis.py; the scan asks for no 5xx
    # and well-formed responses.
    CHECKS = [
        schemathesis.checks.not_a_server_error,
        schemathesis.checks.content_type_conformance,
    ]

    #: path -> why generated input is not sent to it with a session.
    EXCLUDED_PATHS: dict[str, str] = {
        "/api/alphacrafter/recommendations/generate": (
            "runs the whole AlphaCrafter pipeline (150 price fetches) per call; "
            "ten examples exceed the per-test timeout"
        ),
    }
    #: Re-run some operations only, e.g. API_SCAN_ONLY="GET /api/tax/events;POST /api/quant/mc/run".
    ONLY = {op.strip() for op in os.environ.get("API_SCAN_ONLY", "").split(";") if op.strip()}

    def _network_is_isolated() -> bool:
        # Providers use curl_cffi (libcurl) and other native clients that a
        # patched ``socket`` would not stop, so the scan needs a network
        # namespace with no route out: ``unshare -rn`` locally, the same in CI.
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=1).close()
        except OSError:
            return True
        return False

    @pytest.fixture(scope="module")
    def scan_db():
        if not _network_is_isolated():
            pytest.fail("run the API scan without network: unshare -rn .venv/bin/pytest ...", pytrace=False)
        # A file, not :memory: + StaticPool: handlers hand work to background
        # threads, and threads sharing one SQLite connection fail at random.
        workdir = tempfile.mkdtemp(prefix="api-scan-")
        path = os.path.join(workdir, "scan.db")
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
        Base.metadata.create_all(engine)
        _create_ormless_tables(engine)
        # Rebind the app's own sessionmaker: handlers that start a background
        # thread open their sessions from it, not through get_db.
        old_bind = db_module.SessionLocal.kw["bind"]
        db_module.SessionLocal.configure(bind=engine)
        db = db_module.SessionLocal()
        try:
            yield db
        finally:
            db.close()
            db_module.SessionLocal.configure(bind=old_bind)
            engine.dispose()
            shutil.rmtree(workdir, ignore_errors=True)

    @schema.parametrize()
    @settings(max_examples=10, deadline=None, suppress_health_check=list(HealthCheck))
    def test_authenticated_api_scan(case, scan_db):
        if case.operation.path in EXCLUDED_PATHS:
            pytest.skip(f"excluded: {EXCLUDED_PATHS[case.operation.path]}")
        if ONLY and f"{case.operation.method.upper()} {case.operation.path}" not in ONLY:
            pytest.skip("not in API_SCAN_ONLY")
        limiter.reset()
        # A generated logout, password change or user delete must not end the
        # scan's session: re-read (or recreate) the admin before every call.
        scan_db.rollback()
        user = _admin(scan_db)
        response = case.call(cookies={auth.COOKIE_NAME: _cookie(user)})
        if _dependency_unavailable(response):
            return  # no network, no sc wrapper: this 5xx is the right answer
        case.validate_response(response, checks=CHECKS)

    def _dependency_unavailable(response) -> bool:
        """The two documented 5xx answers for a missing external dependency."""
        try:
            error = response.json()["error"]
        except (ValueError, KeyError, TypeError):
            return False
        if response.status_code == 503:
            # The language model is not reachable (LlmUnavailableError).
            return error.get("reason") == "llm_unavailable"
        if response.status_code == 502:
            # The read-only Scalable wrapper is not installed (docs/scalable.md).
            message = error.get("message")
            return isinstance(message, dict) and message.get("code") == "not_installed"
        return False
