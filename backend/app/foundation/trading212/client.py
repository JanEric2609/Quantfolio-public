"""The only place a Trading 212 request is built. Read-only by construction.

Three guards, each enough on its own to stop a write:

1. ``ALLOWED_PATHS``: a fixed list of GET endpoints. Any other path, including
   a ``nextPagePath`` the server returns, raises before a request exists.
2. ``get`` is the only request method; there is no ``post``/``delete``, and
   the transport is called with ``"GET"`` as a literal.
3. The owner creates the API key without the Orders permission
   (``docs/trading212.md``), so the server refuses an order even if one were sent.

Rate limits are per account. Every response carries ``x-ratelimit-remaining``
and ``x-ratelimit-reset`` (Unix time); the client waits for the reset when a
path has no calls left and retries a 429 after it. Calls are sequential.
The credential lives only in the Basic header built here, in memory.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

logger = logging.getLogger(__name__)

BASE_URLS = {
    "live": "https://live.trading212.com",
    "demo": "https://demo.trading212.com",
}
API_PREFIX = "/api/v0"

# Every endpoint Quantfolio may call. All GET. Never add /equity/orders*,
# /equity/history/exports or anything that writes.
ALLOWED_PATHS: frozenset[str] = frozenset({
    "/equity/account/summary",
    "/equity/positions",
    "/equity/history/orders",
    "/equity/history/dividends",
    "/equity/history/transactions",
    "/equity/pies",
    "/equity/metadata/instruments",
})
# GET /equity/pies/{id}: one pie's instruments and settings.
_PIE_DETAIL = re.compile(r"^/equity/pies/\d{1,12}$")

PAGE_LIMIT = 50  # the API's maximum
MAX_PAGES = 400  # 20,000 rows per feed: well past any personal account's history
MAX_RETRIES = 3
MAX_WAIT_SECONDS = 70.0  # the longest documented window is 1 call per 50 s
TIMEOUT_SECONDS = 30.0


class Trading212Error(Exception):
    """A request failed; ``code`` is stable, ``message`` is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Trading212AuthError(Trading212Error):
    """401: the key or secret is wrong, revoked or expired."""


class Trading212ScopeError(Trading212Error):
    """403: the key lacks the permission this endpoint needs (or the IP is not allowed)."""


class Trading212Transient(Trading212Error):
    """Timeout, rate limit not cleared, network or 5xx: try again later."""


class Trading212ProtocolError(Trading212Error):
    """The response was not the JSON the endpoint documents."""


class Trading212ReadOnlyViolation(Trading212Error):
    """Something asked for a path outside the allow-list. A bug on our side."""


def check_path(path: str) -> str:
    """The allow-listed API path (without the ``/api/v0`` prefix), or raise."""
    if not isinstance(path, str) or "?" in path or "#" in path or ".." in path:
        raise Trading212ReadOnlyViolation("path_not_allowed", "Refused a Trading 212 path outside the allow-list")
    if path in ALLOWED_PATHS or _PIE_DETAIL.match(path):
        return path
    raise Trading212ReadOnlyViolation("path_not_allowed", "Refused a Trading 212 path outside the allow-list")


@dataclass
class Trading212Config:
    api_key: str
    api_secret: str
    environment: str = "live"
    timeout_seconds: float = TIMEOUT_SECONDS

    @property
    def base_url(self) -> str:
        return BASE_URLS.get(self.environment, BASE_URLS["live"])


@dataclass
class RateWindow:
    remaining: int | None = None
    reset_at: float | None = None
    limit: int | None = None


# (method, url, headers, params, timeout) -> response. Tests pass a fake.
Transport = Callable[[str, str, dict[str, str], dict[str, Any], float], httpx.Response]


def _httpx_transport(method: str, url: str, headers: dict[str, str], params: dict[str, Any],
                     timeout: float) -> httpx.Response:
    return httpx.request(method, url, headers=headers, params=params, timeout=timeout, follow_redirects=False)


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    try:
        return int(float(headers.get(name, "")))
    except ValueError:
        return None


@dataclass
class Trading212Client:
    config: Trading212Config
    transport: Transport = _httpx_transport
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    windows: dict[str, RateWindow] = field(default_factory=dict)
    calls: int = 0

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self.config.api_key}:{self.config.api_secret}".encode()).decode()
        return f"Basic {token}"

    @staticmethod
    def _window_key(path: str) -> str:
        return "/equity/pies/{id}" if _PIE_DETAIL.match(path) else path

    def _wait_for_window(self, key: str) -> None:
        window = self.windows.get(key)
        if window is None or window.remaining is None or window.remaining > 0 or window.reset_at is None:
            return
        wait = window.reset_at - self.clock()
        if wait > 0:
            self.sleep(min(wait + 0.25, MAX_WAIT_SECONDS))

    def _remember(self, key: str, response: httpx.Response) -> None:
        self.windows[key] = RateWindow(
            remaining=_int_header(response.headers, "x-ratelimit-remaining"),
            reset_at=_int_header(response.headers, "x-ratelimit-reset"),
            limit=_int_header(response.headers, "x-ratelimit-limit"),
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET an allow-listed endpoint and return its JSON."""
        path = check_path(path)
        key = self._window_key(path)
        url = self.config.base_url + API_PREFIX + path
        headers = {"Authorization": self._auth_header(), "Accept": "application/json"}
        for attempt in range(MAX_RETRIES + 1):
            self._wait_for_window(key)
            try:
                self.calls += 1
                response = self.transport("GET", url, headers, dict(params or {}), self.config.timeout_seconds)
            except httpx.HTTPError as exc:
                if attempt < MAX_RETRIES:
                    self.sleep(2.0 * (attempt + 1))
                    continue
                raise Trading212Transient("network", f"Trading 212 did not answer ({type(exc).__name__})") from None
            self._remember(key, response)
            status = response.status_code
            if status == 429 or status == 408 or status >= 500:
                if attempt < MAX_RETRIES:
                    window = self.windows[key]
                    reset_wait = (window.reset_at - self.clock()) if window.reset_at else 0.0
                    self.sleep(min(max(reset_wait + 0.25, 2.0 * (attempt + 1)), MAX_WAIT_SECONDS))
                    continue
                code = "rate_limited" if status == 429 else ("timeout" if status == 408 else "server_error")
                raise Trading212Transient(code, f"Trading 212 answered {status} on {key}")
            if status == 401:
                raise Trading212AuthError(
                    "unauthorized", "Trading 212 refused the API key: check the key and secret, or create a new key")
            if status == 403:
                raise Trading212ScopeError(
                    "forbidden", f"The API key may not read {key}: enable that permission on the key "
                                 "(or allow this server's IP address)")
            if status >= 400:
                raise Trading212ProtocolError("bad_request", f"Trading 212 answered {status} on {key}")
            try:
                return response.json()
            except ValueError:
                raise Trading212ProtocolError("not_json", f"Trading 212 sent no JSON on {key}") from None
        raise Trading212Transient("rate_limited", f"Trading 212 kept refusing {key}")  # pragma: no cover

    def paginate(self, path: str, params: dict[str, Any] | None = None, *,
                 stop: Callable[[list[dict[str, Any]]], bool] | None = None) -> tuple[list[dict[str, Any]], bool]:
        """Every item of a cursor-paged feed, and whether ``MAX_PAGES`` cut it short.

        ``stop(page)`` ends the walk after a page it accepts (an incremental
        sync stops at a page holding what it already stored).
        """
        query: dict[str, Any] = {"limit": PAGE_LIMIT, **(params or {})}
        out: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        for _page in range(MAX_PAGES):
            body = self.get(path, query)
            if not isinstance(body, dict):
                raise Trading212ProtocolError("not_a_page", f"{path} did not return a page")
            items = body.get("items") or []
            if not isinstance(items, list):
                raise Trading212ProtocolError("not_a_page", f"{path} returned items that are not a list")
            out.extend(i for i in items if isinstance(i, dict))
            if stop is not None and stop([i for i in items if isinstance(i, dict)]):
                return out, False
            next_path = body.get("nextPagePath")
            if not next_path or not items:
                return out, False
            query = _next_query(path, str(next_path))
            cursor = str(query.get("cursor"))
            if cursor in seen_cursors:
                return out, False
            seen_cursors.add(cursor)
        return out, True


def _next_query(path: str, next_path: str) -> dict[str, Any]:
    """The query of ``nextPagePath``, after checking it points at the same feed."""
    parts = urlsplit(next_path)
    if parts.scheme or parts.netloc:
        raise Trading212ReadOnlyViolation("path_not_allowed", "Refused a next page on another host")
    next_api_path = parts.path.removeprefix(API_PREFIX)
    if next_api_path != path:
        raise Trading212ReadOnlyViolation("path_not_allowed", "Refused a next page outside the same feed")
    query = dict(parse_qsl(parts.query))
    if "cursor" not in query:
        raise Trading212ProtocolError("bad_next_page", "A next page without a cursor")
    return query
