"""Sliding-window rate limiter for provider API call throttling.

Limiters are per *process* by default. The API and the worker are separate
processes that call the same vendors, so each one alone could stay under a quota
and the pair still exceed it. When ``REDIS_URL`` is configured the shared
limiters are Redis-backed (:class:`RedisRateLimiter`): one sliding window per key
shared by every process. Without it, or when Redis cannot be reached, the
in-process :class:`RateLimiter` applies.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class RateLimitConfig:
    """Configuration for a rate limiter.

    Args:
        max_calls: Maximum number of calls allowed in the window.
        window_seconds: Length of the sliding window in seconds.
        description: Human-readable description (e.g. "Alpha Vantage free tier: 5/min").
    """

    def __init__(self, max_calls: int, window_seconds: int, description: str = ""):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.description = description


class RateLimiter:
    """Sliding-window rate limiter using a deque of timestamps.

    Thread-safe for concurrent multi-threaded access. Uses an internal
    ``threading.Lock`` created in ``__init__`` to protect shared state.
    """

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self._call_times: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self) -> None:
        """Remove timestamps outside the current window."""
        cutoff = time.monotonic() - self.config.window_seconds
        while self._call_times and self._call_times[0] < cutoff:
            self._call_times.popleft()

    @property
    def remaining(self) -> int:
        """Number of calls remaining in the current window."""
        with self._lock:
            self._trim()
            return max(0, self.config.max_calls - len(self._call_times))

    @property
    def is_throttled(self) -> bool:
        """Check if we're currently over the rate limit."""
        return self.remaining <= 0

    def acquire(self) -> float | None:
        """Try to acquire a call slot. Returns a token on success, None if throttled.

        The token must be passed to :meth:`release` so that only the caller's own
        timestamp is removed — avoids a LIFO race when multiple threads hold
        timestamps on the same limiter.
        """
        with self._lock:
            self._trim()
            if len(self._call_times) >= self.config.max_calls:
                return None
            token = time.monotonic()
            self._call_times.append(token)
            return token

    def release(self, token: float) -> None:
        """Undo an acquire by removing the exact token (timestamp).

        Args:
            token: The token returned by :meth:`acquire`.
        """
        with self._lock:
            try:
                self._call_times.remove(token)
            except ValueError:
                pass

    def reset(self) -> None:
        """Reset the rate limiter (e.g. when a daily limit resets)."""
        with self._lock:
            self._call_times.clear()


# -- Redis-backed limiter ------------------------------------------------------

# Atomic sliding-window log: drop entries older than the window, refuse when the
# window is full, else record this call. Redis' own clock (TIME) is used so that
# processes with skewed clocks still share one window. KEYS[1] = window key,
# ARGV = max_calls, window_ms, member. Returns 1 when the call was admitted.
_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now_ms = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - tonumber(ARGV[2]))
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
    return 0
end
redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

# Same trim, then report the free slots without recording a call.
_REMAINING_LUA = """
local t = redis.call('TIME')
local now_ms = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - tonumber(ARGV[2]))
return tonumber(ARGV[1]) - redis.call('ZCARD', KEYS[1])
"""

KEY_PREFIX = "quantfolio:ratelimit:"
# Fail fast: a limiter must never stall a provider call on a dead Redis.
_REDIS_SOCKET_TIMEOUT_S = 0.5
# After a Redis error, stay on the in-process limiter this long before retrying.
_REDIS_RETRY_AFTER_S = 30.0


def redis_limiter_url() -> str | None:
    """The Redis URL when ``REDIS_URL`` is explicitly configured, else ``None``.

    ``Settings.redis_url`` always has a localhost default (used by the passkey
    challenge store, which probes it), so "configured" means the value came from
    the environment or ``.env``: a developer machine that merely has Redis
    running is not silently coupled to it, and neither are the tests.
    """
    from app.foundation.core.config import get_settings

    settings = get_settings()
    url = (settings.redis_url or "").strip()
    return url if url and "redis_url" in settings.model_fields_set else None


_redis_client: Any = None
_redis_client_lock = threading.Lock()


def _get_redis_client() -> Any | None:
    """The process-wide Redis client, or ``None`` when unconfigured/unavailable."""
    global _redis_client
    url = redis_limiter_url()
    if url is None:
        return None
    with _redis_client_lock:
        if _redis_client is None:
            try:
                import redis

                _redis_client = redis.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=_REDIS_SOCKET_TIMEOUT_S,
                    socket_timeout=_REDIS_SOCKET_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 - redis missing or an unusable URL: stay in-process
                logger.warning("REDIS_URL is set but a Redis client could not be created; "
                               "provider rate limits stay per-process", exc_info=True)
                return None
        return _redis_client


def reset_shared_state() -> None:
    """Forget shared limiters and the Redis client (tests; config reload)."""
    global _redis_client
    with _shared_limiters_lock:
        _shared_limiters.clear()
    with _redis_client_lock:
        _redis_client = None


class RedisRateLimiter(RateLimiter):
    """:class:`RateLimiter` whose window lives in Redis, shared across processes.

    Same interface and semantics (sliding window, ``acquire``/``release`` with a
    token). If Redis errors, the call is served by the inherited in-process
    window and Redis is not retried for ``_REDIS_RETRY_AFTER_S`` seconds, so an
    outage degrades to today's per-process behaviour instead of blocking or
    failing provider calls.
    """

    def __init__(self, config: RateLimitConfig, key: str, client_factory: Any = None):
        super().__init__(config)
        self._key = f"{KEY_PREFIX}{key}"
        self._client_factory = client_factory or _get_redis_client
        self._window_ms = int(config.window_seconds * 1000)
        self._down_until = 0.0

    # -- plumbing --------------------------------------------------------------

    def _client(self) -> Any | None:
        if time.monotonic() < self._down_until:
            return None
        return self._client_factory()

    def _degrade(self, exc: Exception) -> None:
        self._down_until = time.monotonic() + _REDIS_RETRY_AFTER_S
        logger.warning(
            "Redis rate limiter %s unavailable (%s); using the in-process limiter for %.0fs",
            self._key, exc, _REDIS_RETRY_AFTER_S,
        )

    # -- RateLimiter interface -------------------------------------------------

    @property
    def remaining(self) -> int:
        client = self._client()
        if client is not None:
            try:
                return max(0, int(client.eval(
                    _REMAINING_LUA, 1, self._key, self.config.max_calls, self._window_ms,
                )))
            except Exception as exc:  # noqa: BLE001
                self._degrade(exc)
        return super().remaining

    def acquire(self) -> Any:
        client = self._client()
        if client is not None:
            token = uuid.uuid4().hex
            try:
                admitted = client.eval(
                    _ACQUIRE_LUA, 1, self._key, self.config.max_calls, self._window_ms, token,
                )
                return token if int(admitted) == 1 else None
            except Exception as exc:  # noqa: BLE001
                self._degrade(exc)
        return super().acquire()

    def release(self, token: Any) -> None:
        if isinstance(token, str):  # a Redis member
            client = self._client()
            if client is not None:
                try:
                    client.zrem(self._key, token)
                except Exception as exc:  # noqa: BLE001
                    self._degrade(exc)
            return
        super().release(token)

    def reset(self) -> None:
        super().reset()
        client = self._client()
        if client is not None:
            try:
                client.delete(self._key)
            except Exception as exc:  # noqa: BLE001
                self._degrade(exc)


# Centralized reference: per-provider rate limits for every provider in the chain.
# Providers create their own limiters via get_shared_limiter() using these values.
PROVIDER_RATE_LIMITS: dict[str, list[tuple[int, int, str]]] = {
    "finnhub":     [(60, 60, "Finnhub free tier: 60/min")],
    # Self-imposed guardrail (docs/adr/0010-openbb-per-purpose-routing.md):
    # unlike the paid-provider limits above, this isn't a published API quota
    # — it protects a single self-hosted LXC container from being hammered by
    # scheduled jobs (macro refresh, provider health probes) plus any
    # interactive traffic that falls through to it, now that it does real
    # work (get_fred_series) beyond the previously-disabled-by-default
    # quote/history fallback it always had.
    "openbb":      [(30, 60, "OpenBB self-hosted LXC: 30/min self-imposed guardrail")],
    "twelvedata":  [(8, 60, "Twelve Data free tier: 8/min"), (800, 86400, "Twelve Data free tier: 800/day")],
    "alpaca":      [(200, 60, "Alpaca free tier: 200/min")],
    "databento":   [],  # 250K msg/month, managed by provider SDK
    "tiingo":      [(50, 3600, "Tiingo: 50/hour"), (1000, 86400, "Tiingo: 1000/day soft limit")],
    "alphavantage": [(5, 60, "Alpha Vantage free tier: 5/min"), (25, 86400, "Alpha Vantage free tier: 25/day")],
    "fred":        [(120, 60, "FRED free tier: 120/min")],
    "eod":         [(20, 86400, "EODHD free tier: 20/day")],
    "ecb_sdw":     [],  # no practical rate limit
    "massive":     [(5, 60, "Polygon.io free tier: 5/min")],
    "yfinance":    [(2000, 3600, "yfinance soft throttle: ~2000/hr")],
}


def create_limiters(name: str) -> list[RateLimiter]:
    """Create rate limiters for a provider from PROVIDER_RATE_LIMITS.

    Returns a list of RateLimiter instances for the provider.
    Each entry in PROVIDER_RATE_LIMITS[name] is (max_calls, window_s, desc).
    """
    configs = PROVIDER_RATE_LIMITS.get(name, [])
    limiters: list[RateLimiter] = []
    for i, (max_calls, window_s, desc) in enumerate(configs):
        limiter_key = f"{name}/{i}" if len(configs) > 1 else name
        limiters.append(get_shared_limiter(limiter_key,
            RateLimitConfig(max_calls=max_calls, window_seconds=window_s, description=desc)))
    return limiters


def acquire_all(limiters: list[RateLimiter]) -> bool:
    """Atomically acquire all rate limiters.

    Tries to acquire each limiter. If any fails, releases already-acquired
    limiters so no quota is leaked. Returns True iff all were acquired.
    """
    acquired: list[tuple[RateLimiter, Any]] = []
    for limiter in limiters:
        token = limiter.acquire()
        if token is not None:
            acquired.append((limiter, token))
        else:
            for limiter, token in acquired:
                limiter.release(token)
            return False
    return True


_shared_limiters: dict[str, RateLimiter] = {}
_shared_limiters_lock = threading.Lock()


def get_shared_limiter(key: str, config: RateLimitConfig) -> RateLimiter:
    """Get or create a shared rate limiter by key.

    Shared limiters enforce rate limits across all consumers that use the same key
    — across processes too when ``REDIS_URL`` is configured (see
    :class:`RedisRateLimiter`), otherwise within this process.
    """
    with _shared_limiters_lock:
        if key not in _shared_limiters:
            if redis_limiter_url() is not None:
                _shared_limiters[key] = RedisRateLimiter(config, key)
            else:
                _shared_limiters[key] = RateLimiter(config)
        return _shared_limiters[key]


_shared_async_limiters: dict[str, asyncio.Semaphore] = {}
_shared_async_limiters_lock = threading.Lock()


def get_shared_async_limiter(key: str, value: int = 1) -> asyncio.Semaphore:
    """Get or create a shared asyncio semaphore by key.

    Async counterpart of :func:`get_shared_limiter`: serializes concurrent
    coroutines that use the same key. Creation is guarded by a
    ``threading.Lock`` because callers may run on different threads (e.g.
    scheduler threads); runtime waiting is done via the semaphore itself.
    """
    with _shared_async_limiters_lock:
        if key not in _shared_async_limiters:
            _shared_async_limiters[key] = asyncio.Semaphore(value)
        return _shared_async_limiters[key]
