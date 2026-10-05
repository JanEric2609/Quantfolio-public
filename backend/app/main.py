from contextlib import asynccontextmanager
import logging
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.interface.api import routers
from app.interface.middleware import CrossSiteRequestGuard
from app.foundation.core.config import _DEFAULT_JWT_SECRET, get_settings
from app.foundation.core.db import create_all, SessionLocal
from app.foundation.core.logging_setup import configure_logging
from app.foundation.core.security import limiter
from app.interface.cors import DynamicCORSMiddleware, env_fallback_origins, resolve_origins_from_db
from app.lab.alphacrafter.orchestrator import reap_stale_job_runs
from app.decision.discover.orchestrator import reap_stale_discover_runs
from app.foundation.settings import get_public_settings, resolve_frontend_origins
from app.foundation.setup_token import announce_setup_token_if_needed

logger = logging.getLogger("quantfolio")

# The code default and the .env.example value: both are public, so either one
# in a real deployment means anyone can mint a session cookie.
_PLACEHOLDER_JWT_SECRETS = frozenset({_DEFAULT_JWT_SECRET, "change-this-before-use"})
_MIN_JWT_SECRET_LENGTH = 32


def _startup_security_checks(settings) -> None:
    """Raise RuntimeError on misconfigured secrets in non-local environments.

    Fails closed: a Postgres DSN is treated as a real deployment even if APP_ENV
    is (mis)set to local, so a misconfigured prod can never boot with the weak
    JWT default or a JWT-derived encryption key (issue #129).
    """
    if not settings.is_local or settings.is_postgres:
        if settings.jwt_secret in _PLACEHOLDER_JWT_SECRETS:
            raise RuntimeError(
                "JWT_SECRET must be changed from the default value in non-local environments. "
                "Set the JWT_SECRET environment variable to a strong random secret."
            )
        if len(settings.jwt_secret) < _MIN_JWT_SECRET_LENGTH:
            # A warning, not a refusal: an existing deployment must not stop
            # booting on upgrade. Generate one with `openssl rand -hex 32`.
            logger.warning(
                "JWT_SECRET is shorter than %d characters; sessions signed with it are brute-forceable. "
                "Replace it with `openssl rand -hex 32` (this signs everyone out).",
                _MIN_JWT_SECRET_LENGTH,
            )
        if not settings.encryption_key:
            raise RuntimeError(
                "ENCRYPTION_KEY must be set in non-local environments. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )


_WEBAUTHN_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _check_webauthn_rp_id(db, settings) -> None:
    """Pure-warning passkey configuration check (audit §8).

    Resolves webauthn_rp_id exactly like api/auth.py:_webauthn_config (public
    setting → app.foundation.core.config default "localhost") and compares it against the
    resolved frontend_origin hosts. WebAuthn credentials are bound to the RP
    ID, so a 'localhost' rp_id behind a remote hostname silently breaks every
    passkey registration/authentication. Never mutates settings, never raises.
    """
    public = get_public_settings(db)
    rp_id = str(public.get("webauthn_rp_id") or settings.webauthn_rp_id)
    if rp_id != "localhost":
        return
    origins = resolve_frontend_origins(db)
    if not origins:
        logger.debug("webauthn rp_id check skipped: no frontend_origin configured")
        return
    for origin in origins:
        host = (urlparse(origin).hostname or "").lower()
        if not host:
            logger.debug("webauthn rp_id check skipped: unparseable frontend_origin %r", origin)
            continue
        if host in _WEBAUTHN_LOCAL_HOSTS:
            continue
        logger.warning(
            "Passkeys will FAIL: webauthn_rp_id is 'localhost' but the app is served from %s "
            "— set webauthn_rp_id to that hostname in Control Center → Security (audit §8).",
            origin,
        )


def _check_critical_tables(db) -> None:
    """Loud startup probe for the analytics hypertables (issue root cause).

    If bar_prices / regime_snapshots / provider_health_history are missing the
    deployment is broken (migrations not applied to head) — surface it as a
    CRITICAL log line so ops sees the real cause instead of opaque API errors.

    Existence isn't sufficient (ADR 0014): a table can exist with no ``id``
    default and no unique index, in which case every write silently fails.
    Also probe those write invariants so that class of regression is loud too.
    """
    from app.foundation.data_backbone.health_check import check_critical_tables, check_write_invariants

    result = check_critical_tables(db)
    if not result["ok"]:
        logger.critical(
            "CRITICAL: Analytics tables missing (%s). Run 'alembic upgrade head' on the "
            "app LXC to create them (bar_prices, regime_snapshots, provider_health_history). "
            "QuantLab, regime gating, and price backfill will not function until migrations "
            "are applied.",
            ", ".join(result["missing"]),
        )

    invariants = check_write_invariants(db)
    if not invariants["ok"]:
        logger.critical(
            "CRITICAL: Analytics hypertable write path broken — missing id default on %s, "
            "missing unique index on %s. Every insert into these tables is failing "
            "(NotNullViolation/InvalidColumnReference). Run migration "
            "0106_restore_hypertable_writes (ADR 0014) on the app LXC.",
            ", ".join(invariants["missing_id_default"]) or "none",
            ", ".join(invariants["missing_unique_index"]) or "none",
        )


def _announce_first_run_setup() -> None:
    """While no account exists, tell the operator how to register (the one-time setup token)."""
    from app.foundation.auth import has_users

    db = SessionLocal()
    try:
        announce_setup_token_if_needed(has_users(db))
    finally:
        db.close()


# Interactive API docs and the schema are for development; a production API does
# not hand its full route list to anonymous callers. (scripts/export_openapi.py
# calls app.openapi() directly and is unaffected.)
_PRODUCTION_ENVS = frozenset({"production", "prod"})


def _docs_enabled(settings) -> bool:
    return settings.app_env.strip().lower() not in _PRODUCTION_ENVS


def _allowed_hosts(settings) -> list[str]:
    return [host.strip() for host in (settings.allowed_hosts or "").split(",") if host.strip()]


def _current_frontend_origins() -> list[str]:
    """Allowed frontend origins as configured right now (DB -> env -> default)."""
    try:
        return resolve_origins_from_db()
    except Exception:
        return env_fallback_origins()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    _startup_security_checks(settings)
    if settings.app_env == "local" or settings.database_url.startswith("sqlite"):
        create_all()

    try:
        _announce_first_run_setup()
    except Exception:
        logger.warning("setup-token startup check skipped: database unavailable", exc_info=True)

    try:
        db = SessionLocal()
        try:
            _check_webauthn_rp_id(db, settings)
        finally:
            db.close()
    except Exception:
        logger.debug("webauthn rp_id startup check skipped: settings unavailable", exc_info=True)

    try:
        db = SessionLocal()
        try:
            reap_stale_job_runs(db)
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to reap stale AlphaCrafter jobs")

    try:
        db = SessionLocal()
        try:
            reap_stale_discover_runs(db)
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to reap stale discover runs")

    try:
        db = SessionLocal()
        try:
            _check_critical_tables(db)
        finally:
            db.close()
    except Exception:
        logger.warning("critical-table startup check skipped", exc_info=True)

    yield


def create_app() -> FastAPI:
    settings = get_settings()
    docs = _docs_enabled(settings)
    app = FastAPI(
        title="QuantFolio API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.limiter = limiter

    # CORS origins follow the ``frontend_origin`` setting live (DB → env →
    # default, TTL-cached per request); see app/interface/cors.py. Nothing is
    # read from the database at import time.
    app.add_middleware(
        DynamicCORSMiddleware,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class SecurityHeadersMiddleware:
        def __init__(self, app_inner):
            self.app = app_inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)

            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    header_names = {h[0] for h in headers}

                    security_headers = [
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                    ]
                    for name, value in security_headers:
                        if name not in header_names:
                            headers.append((name, value))

                    csp = (
                        b"default-src 'self'; "
                        b"script-src 'self'; "
                        b"style-src 'self' 'unsafe-inline'; "
                        b"img-src 'self' data: blob:; "
                        b"connect-src 'self'"
                    )
                    if b"content-security-policy" not in header_names:
                        headers.append((b"content-security-policy", csp))

                    message["headers"] = headers
                return await send(message)

            return await self.app(scope, receive, send_wrapper)

    app.add_middleware(SecurityHeadersMiddleware)
    # Cookie-authenticated unsafe requests the browser marks cross-site are refused.
    app.add_middleware(CrossSiteRequestGuard, allowed_origins=_current_frontend_origins)
    # Optional Host allow-list (ALLOWED_HOSTS); off when unset.
    allowed_hosts = _allowed_hosts(settings)
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_error(_request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"error": {"code": 429, "message": "Too many requests. Please slow down."}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.status_code, "message": exc.detail}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": 422, "message": "Validation failed", "details": exc.errors()}},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception):
        logger.exception("Unhandled API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": 500, "message": "Internal server error"}},
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "quantfolio-api"}

    @app.get("/health/ready")
    def ready():
        return {
            "status": "ready",
            "database": "configured",
            "redis": "configured" if settings.redis_url else "disabled",
        }

    for router in routers:
        app.include_router(router)
    return app


app = create_app()
