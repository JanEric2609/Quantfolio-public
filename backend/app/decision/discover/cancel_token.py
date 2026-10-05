"""CancelToken for cooperative cancellation via DB status polling."""
from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoverRun

logger = logging.getLogger(__name__)

DEFAULT_TTL = 5.0       # seconds between DB status checks
DEFAULT_TIMEOUT = 30.0  # minutes before auto-timeout


class CancellationError(Exception):
    """Raised when the run should stop (cancellation or timeout)."""
    def __init__(self, message: str, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


class CancelToken:
    """Cooperative cancellation token backed by DB status polling.

    Checks the DiscoverRun status every ``ttl`` seconds. If the status
    is ``cancelled`` or ``cancellation_requested``, raises
    ``CancellationError``. Also enforces a wall-clock ``timeout_minutes``.
    """

    def __init__(
        self,
        db: Session,
        run_id: str,
        *,
        ttl: float = DEFAULT_TTL,
        timeout_minutes: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._ttl = ttl
        self._timeout_minutes = timeout_minutes
        self._deadline = time.time() + timeout_minutes * 60.0
        self._last_check = 0.0

    def raise_if_cancelled(self) -> None:
        """Check cancellation — raises CancellationError if stopped/timeout."""
        now = time.time()

        # Wall-clock timeout
        if now >= self._deadline:
            raise CancellationError(f"Timeout ({self._timeout_minutes:.0f} min)", timeout=True)

        # TTL gate — skip DB if we checked recently
        if now - self._last_check < self._ttl:
            return
        self._last_check = now

        # Use a dedicated session for the status poll. The pipeline's main
        # session can be left in an aborted (poisoned) transaction by a failed
        # statement in a prior stage; a SELECT on it raises
        # InFailedSqlTransaction instead of reporting the real cancellation
        # state, which both hangs cancellation and masks the underlying error.
        # A fresh session is always queryable.
        from app.foundation.core.db import SessionLocal

        db = SessionLocal()
        try:
            run = db.get(DiscoverRun, self._run_id)
        finally:
            db.close()
        if run is None:
            raise CancellationError("Run deleted while in progress")

        if run.status in ("completed", "failed"):
            raise CancellationError(f"Run finished with status '{run.status}'")

        if run.status == "cancelled":
            raise CancellationError("Run cancelled by user")

        if run.status == "cancellation_requested":
            raise CancellationError("Run cancellation requested")
