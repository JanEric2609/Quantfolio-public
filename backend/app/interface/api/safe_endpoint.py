"""Decorator to wrap FastAPI endpoint functions in try/except for 500 hardening.

Usage:
    from app.interface.api.safe_endpoint import safe_endpoint

    @router.get("/...")
    @safe_endpoint
    def my_endpoint(...):
        ...
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar, overload

from fastapi import HTTPException

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


@overload
def safe_endpoint(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]: ...


@overload
def safe_endpoint(func: Callable[P, R]) -> Callable[P, R]: ...


def safe_endpoint(func: Any) -> Any:
    """Wrap an endpoint so that all unhandled exceptions become 500 responses.

    HTTPException and its subclasses are re-raised (FastAPI handles them).
    All other exceptions are caught, logged, and returned as HTTP 500.
    """

    @functools.wraps(func)
    async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
        try:
            return await func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Unhandled error in %s: %s", func.__name__, exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @functools.wraps(func)
    def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
        try:
            return func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Unhandled error in %s: %s", func.__name__, exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper
