"""Provider rate limits shared across processes through Redis (audit D2).

The API and the worker are separate processes calling the same vendors; per-process
limiters let the pair exceed a quota. With ``REDIS_URL`` configured the shared
limiters are :class:`RedisRateLimiter`; otherwise (or when Redis is down) the
in-process :class:`RateLimiter` applies.

No Redis stand-in package is installed, so the Redis behaviour is tested against a
real ``redis-server`` started on a free port (skipped when the binary is absent);
the configuration and fallback paths need no server at all.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.foundation.core import config as core_config
from app.foundation.providers import rate_limiter as rl
from app.foundation.providers.rate_limiter import (
    RateLimitConfig,
    RateLimiter,
    RedisRateLimiter,
    acquire_all,
    get_shared_limiter,
    redis_limiter_url,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_limiter_state():
    rl.reset_shared_state()
    yield
    rl.reset_shared_state()
    core_config.get_settings.cache_clear()


# -- configuration and fallback: no server needed ---------------------------------


def test_not_configured_by_default_under_the_test_environment():
    assert redis_limiter_url() is None  # conftest pins REDIS_URL to ""


def test_url_counts_as_configured_only_when_set_explicitly(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    # The premise: the Settings default (localhost) is always truthy but is not
    # "configured" -- only a value from the environment or .env is.
    defaults = core_config.Settings(_env_file=None)
    assert defaults.redis_url and "redis_url" not in defaults.model_fields_set

    monkeypatch.setenv("REDIS_URL", "redis://cache.internal:6380/2")
    core_config.get_settings.cache_clear()
    assert redis_limiter_url() == "redis://cache.internal:6380/2"

    monkeypatch.setenv("REDIS_URL", "")
    core_config.get_settings.cache_clear()
    assert redis_limiter_url() is None


def test_shared_limiter_is_in_process_without_redis():
    limiter = get_shared_limiter("unit/none", RateLimitConfig(2, 60))
    assert type(limiter) is RateLimiter
    assert get_shared_limiter("unit/none", RateLimitConfig(99, 1)) is limiter  # same key, same limiter


def test_shared_limiter_is_redis_backed_when_configured(monkeypatch):
    monkeypatch.setattr(rl, "redis_limiter_url", lambda: "redis://localhost:6379/0")
    limiter = get_shared_limiter("unit/redis", RateLimitConfig(2, 60))
    assert isinstance(limiter, RedisRateLimiter)
    assert get_shared_limiter("unit/redis", RateLimitConfig(2, 60)) is limiter


class _BrokenClient:
    def __init__(self):
        self.calls = 0

    def eval(self, *_args):
        self.calls += 1
        raise ConnectionError("redis down")

    def zrem(self, *_args):
        self.calls += 1
        raise ConnectionError("redis down")

    delete = zrem


def test_redis_outage_degrades_to_the_in_process_window(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: clock[0])
    client = _BrokenClient()
    limiter = RedisRateLimiter(RateLimitConfig(3, 60), "unit/outage", client_factory=lambda: client)

    tokens = [limiter.acquire() for _ in range(5)]

    assert [t is not None for t in tokens] == [True, True, True, False, False]  # local window still caps at 3
    assert client.calls == 1  # one failed attempt, then no more Redis calls during the cool-down
    assert limiter.remaining == 0

    limiter.release(tokens[0])  # a local float token goes back to the local window
    assert limiter.remaining == 1

    clock[0] += rl._REDIS_RETRY_AFTER_S + 1
    limiter.acquire()
    assert client.calls == 2  # cool-down over: Redis is probed again


def test_unreachable_client_factory_never_raises():
    limiter = RedisRateLimiter(RateLimitConfig(1, 60), "unit/nofactory", client_factory=lambda: None)
    assert limiter.acquire() is not None
    assert limiter.acquire() is None


# -- real Redis ------------------------------------------------------------------

redis_server = shutil.which("redis-server")
needs_redis = pytest.mark.skipif(redis_server is None, reason="redis-server binary not installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def redis_url(tmp_path):
    import redis

    port = _free_port()
    proc = subprocess.Popen(
        [redis_server, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
         "--dir", str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    probe = redis.Redis.from_url(url, socket_connect_timeout=0.2)
    for _ in range(100):
        try:
            probe.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.05)
    else:  # pragma: no cover
        proc.kill()
        pytest.fail("redis-server did not start")
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _limiter(url: str, key: str, max_calls: int, window_s: int | float) -> RedisRateLimiter:
    import redis

    # A separate client per limiter stands in for a separate process.
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
    return RedisRateLimiter(RateLimitConfig(max_calls, window_s), key, client_factory=lambda: client)


@needs_redis
def test_two_limiters_on_one_key_share_a_single_window(redis_url):
    api, worker = _limiter(redis_url, "shared", 4, 60), _limiter(redis_url, "shared", 4, 60)

    assert api.acquire() and api.acquire() and worker.acquire() and worker.acquire()
    assert api.acquire() is None
    assert worker.acquire() is None
    assert api.remaining == worker.remaining == 0


@needs_redis
def test_different_keys_do_not_interfere(redis_url):
    one, two = _limiter(redis_url, "k1", 1, 60), _limiter(redis_url, "k2", 1, 60)
    assert one.acquire() and two.acquire()
    assert one.acquire() is None and two.acquire() is None


@needs_redis
def test_release_frees_exactly_the_callers_slot(redis_url):
    limiter = _limiter(redis_url, "release", 2, 60)
    first, second = limiter.acquire(), limiter.acquire()
    assert limiter.acquire() is None

    limiter.release(first)

    assert limiter.remaining == 1
    assert limiter.acquire() is not None
    assert limiter.acquire() is None
    limiter.release(second)
    assert limiter.remaining == 1


@needs_redis
def test_window_slides_and_the_key_expires(redis_url):
    import redis

    limiter = _limiter(redis_url, "slide", 2, 1)
    assert limiter.acquire() and limiter.acquire()
    assert limiter.acquire() is None

    client = redis.Redis.from_url(redis_url)
    assert 0 < client.pttl(f"{rl.KEY_PREFIX}slide") <= 1000

    time.sleep(1.15)
    assert limiter.remaining == 2
    assert limiter.acquire() is not None


@needs_redis
def test_admission_is_atomic_under_concurrency(redis_url):
    limiters = [_limiter(redis_url, "atomic", 25, 60) for _ in range(4)]
    admitted: list[int] = []
    lock = threading.Lock()

    def hammer(limiter: RedisRateLimiter) -> None:
        got = sum(1 for _ in range(20) if limiter.acquire() is not None)
        with lock:
            admitted.append(got)

    threads = [threading.Thread(target=hammer, args=(lim,)) for lim in limiters]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(admitted) == 25  # 80 attempts across 4 "processes", never more than the quota


@needs_redis
def test_reset_clears_the_shared_window(redis_url):
    limiter = _limiter(redis_url, "reset", 1, 60)
    assert limiter.acquire()
    assert limiter.acquire() is None
    limiter.reset()
    assert limiter.acquire() is not None


@needs_redis
def test_acquire_all_releases_the_slots_it_took_when_a_later_limiter_refuses(redis_url):
    minute, day = _limiter(redis_url, "multi/0", 5, 60), _limiter(redis_url, "multi/1", 1, 86400)
    assert day.acquire() is not None  # the daily quota is already used up

    assert acquire_all([minute, day]) is False

    assert minute.remaining == 5  # the minute slot taken first was handed back


@needs_redis
def test_a_dead_redis_mid_run_falls_back_instead_of_failing(redis_url):
    import redis

    dead = redis.Redis.from_url(
        f"redis://127.0.0.1:{_free_port()}/0", decode_responses=True,
        socket_connect_timeout=0.2, socket_timeout=0.2,
    )
    limiter = RedisRateLimiter(RateLimitConfig(2, 60), "dead", client_factory=lambda: dead)
    assert limiter.acquire() is not None
    assert limiter.acquire() is not None
    assert limiter.acquire() is None  # still limited, by the in-process window


@needs_redis
def test_a_second_process_shares_the_quota(redis_url):
    """The API/worker case: another interpreter spends part of the window."""
    script = textwrap.dedent(
        f"""
        import redis
        from app.foundation.providers.rate_limiter import RateLimitConfig, RedisRateLimiter

        client = redis.Redis.from_url({redis_url!r}, decode_responses=True)
        limiter = RedisRateLimiter(RateLimitConfig(5, 60), "xproc", client_factory=lambda: client)
        print(sum(1 for _ in range(3) if limiter.acquire() is not None))
        """
    )
    child = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": str(BACKEND_DIR)},
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip().splitlines()[-1] == "3"

    mine = _limiter(redis_url, "xproc", 5, 60)
    assert sum(1 for _ in range(4) if mine.acquire() is not None) == 2  # 5 - the child's 3


@needs_redis
def test_shared_limiter_factory_uses_redis_end_to_end(redis_url, monkeypatch):
    import redis

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    monkeypatch.setattr(rl, "redis_limiter_url", lambda: redis_url)
    monkeypatch.setattr(rl, "_get_redis_client", lambda: client)

    first = get_shared_limiter("e2e/provider", RateLimitConfig(1, 60))
    assert isinstance(first, RedisRateLimiter)
    assert first.acquire() is not None
    # A fresh process-level registry (a new process) still sees the spent quota.
    rl.reset_shared_state()
    second = get_shared_limiter("e2e/provider", RateLimitConfig(1, 60))
    assert second is not first
    assert second.acquire() is None
