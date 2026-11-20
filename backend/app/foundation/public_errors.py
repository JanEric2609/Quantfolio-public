"""Error text that is safe to send to the browser.

An exception's message can carry an endpoint URL, a provider's response, a
file path or a SQL fragment. What reaches the API is what failed and the
error's type; the message itself goes to the server log (CodeQL
py/stack-trace-exposure, public repository 2026-11-20).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_ERROR_KEYS = frozenset({"error", "message"})


def public_error(what: str, exc: BaseException, *, log: logging.Logger | None = None) -> str:
    """``"<what> (<ErrorType>); details are in the server log"``, after logging the details."""
    (log or logger).warning("%s: %s", what, exc)
    return f"{what} ({type(exc).__name__}); details are in the server log"


def scrub_failures(result: Any) -> Any:
    """A copy of *result* with the ``error``/``message`` text of every failed entry replaced.

    An entry is failed when it says ``"success": False`` or ``"status"`` is
    ``failed``/``error``, or when it carries an ``error`` key at all.
    """
    if isinstance(result, list):
        return [scrub_failures(v) for v in result]
    if not isinstance(result, dict):
        return result
    failed = (
        result.get("success") is False
        or str(result.get("status") or "").lower() in ("failed", "error")
        or "error" in result
    )
    out: dict[str, Any] = {}
    for key, value in result.items():
        if failed and key in _ERROR_KEYS and isinstance(value, str):
            out[key] = "failed; details are in the server log"
        else:
            out[key] = scrub_failures(value)
    return out
