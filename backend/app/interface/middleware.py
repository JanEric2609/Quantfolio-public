"""Transport-level request guards (ASGI middleware).

``CrossSiteRequestGuard`` protects cookie-authenticated endpoints: the session
cookie is ``SameSite=Lax``, which still lets a same-site page (a sibling
subdomain, another port on the same host) send credentialed unsafe requests,
and plain ``SameSite`` does nothing for body-less POSTs a browser will submit
as a simple request. So an unsafe request the browser itself labels
cross-site is refused.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _authority(origin_or_host: str) -> str:
    """``host[:port]`` of an Origin (``https://h:8443``) or a Host header, lower-cased."""
    value = origin_or_host.strip().lower()
    if "://" in value:
        return urlsplit(value).netloc
    return value


class CrossSiteRequestGuard:
    """Reject unsafe methods the browser marks as cross-site.

    A request is refused when it carries

    * ``Sec-Fetch-Site: cross-site``, or
    * an ``Origin`` that is neither the origin the page was loaded from (same
      authority as the ``Host`` header, which a browser sets to the target and a
      page cannot forge) nor one of the allowed frontend origins.

    Requests with neither header pass: curl, the test client, and Telegram's
    webhook (which authenticates with its own secret header) never send them,
    and a browser always does for a cross-site request.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Callable[[], Iterable[str]]) -> None:
        self.app = app
        self._allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() not in UNSAFE_METHODS:
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if self._is_cross_site(headers):
            logger.warning(
                "Blocked cross-site %s %s (Sec-Fetch-Site=%r, Origin=%r)",
                scope.get("method"),
                scope.get("path"),
                headers.get("sec-fetch-site"),
                headers.get("origin"),
            )
            body = json.dumps(
                {"error": {"code": 403, "message": "Cross-site request blocked"}}
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)

    def _is_cross_site(self, headers: dict[str, str]) -> bool:
        if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
            return True
        origin = headers.get("origin")
        if origin is None:
            return False
        origin = origin.strip()
        host = headers.get("host", "").strip()
        if origin.lower() != "null" and host and _authority(origin) == _authority(host):
            return False
        try:
            allowed = {o.strip().rstrip("/").lower() for o in self._allowed_origins() if o.strip()}
        except Exception:
            logger.warning("Could not resolve allowed origins for the cross-site guard", exc_info=True)
            allowed = set()
        return origin.rstrip("/").lower() not in allowed
