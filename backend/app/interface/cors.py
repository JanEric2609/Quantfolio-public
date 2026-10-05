"""CORS middleware whose allowed origins follow the ``frontend_origin`` setting live.

``docs/deployment.md`` promises that changing the frontend origin in the Control
Center applies without a restart, but the origins used to be read once in
``create_app()`` and frozen into a static ``CORSMiddleware``. This subclass keeps
Starlette's preflight/simple-response behaviour (credentials, methods, headers,
``Vary: Origin``) and only replaces the origin check: the allowed list is
resolved per request through the settings resolver (DB → env → default) behind a
short TTL cache, refreshed off the event loop, and dropped immediately when the
setting is written in this process.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("quantfolio")

# How long a resolved origin list is reused. Short enough that a Control Center
# change is live within seconds even if another process wrote it; a write in
# this process invalidates sooner via ``settings_cache_generation``.
ORIGINS_TTL_SECONDS = 5.0


def split_origins(value: str) -> list[str]:
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def env_fallback_origins() -> list[str]:
    """Origins from the environment/config alone, for an unreachable or unmigrated DB."""
    from app.foundation.core.config import get_settings

    return split_origins(os.environ.get("FRONTEND_ORIGIN", get_settings().frontend_origin))


def resolve_origins_from_db() -> list[str]:
    """Resolve the allowed origins the way every other consumer does (DB → env → default)."""
    from app.foundation.core.db import SessionLocal
    from app.foundation.settings import resolve_frontend_origins

    db = SessionLocal()
    try:
        return resolve_frontend_origins(db)
    finally:
        db.close()


def _settings_generation() -> int:
    from app.foundation.settings import settings_cache_generation

    return settings_cache_generation()


class DynamicCORSMiddleware(CORSMiddleware):
    """``CORSMiddleware`` that re-resolves ``allow_origins`` per request (TTL-cached)."""

    def __init__(
        self,
        app: ASGIApp,
        origin_resolver: Callable[[], Sequence[str]] = resolve_origins_from_db,
        fallback_origins: Callable[[], Sequence[str]] = env_fallback_origins,
        ttl_seconds: float | None = None,
        **kwargs,
    ) -> None:
        # ``allow_origins`` stays empty for the base class (it only feeds the
        # static "*" shortcut); the live list is kept in ``self.allow_origins``
        # below and consulted by ``is_allowed_origin``.
        super().__init__(app, allow_origins=(), **kwargs)
        self._origin_resolver = origin_resolver
        self._fallback_origins = fallback_origins
        self._ttl = ORIGINS_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self.allow_origins = list(fallback_origins())
        self._resolved_at: float | None = None
        self._resolved_generation = -1

    def _is_stale(self) -> bool:
        if self._resolved_at is None:
            return True
        if _settings_generation() != self._resolved_generation:
            return True
        return time.monotonic() - self._resolved_at >= self._ttl

    def _resolve_blocking(self) -> tuple[list[str], int]:
        generation = _settings_generation()
        try:
            origins = list(self._origin_resolver())
        except Exception:
            # Unmigrated/unreachable DB must never take CORS (and with it the
            # whole API) down: keep the last known list, or the env default.
            logger.debug("CORS origin resolution failed; keeping the previous list", exc_info=True)
            origins = self.allow_origins if self._resolved_at is not None else list(self._fallback_origins())
        return origins, generation

    async def _refresh_origins(self) -> None:
        if not self._is_stale():
            return
        # Concurrent requests at expiry may each resolve once; that is cheap
        # (the settings resolver is itself cached) and needs no lock.
        origins, generation = await run_in_threadpool(self._resolve_blocking)
        self.allow_origins = origins
        self._resolved_generation = generation
        self._resolved_at = time.monotonic()

    def is_allowed_origin(self, origin: str) -> bool:
        # A literal "*" in the setting keeps meaning "any origin" (credentialed
        # responses still echo the concrete origin, as before).
        return "*" in self.allow_origins or origin in self.allow_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only cross-origin requests carry an Origin header; everything else
        # never consults the list, so it never pays for a refresh.
        if scope["type"] == "http" and Headers(scope=scope).get("origin") is not None:
            await self._refresh_origins()
        await super().__call__(scope, receive, send)
