"""Data models and exception classes for DKB FinTS integration.

Extracted from the monolithic dkb.py. Contains the sync session state
machine dataclass, type aliases, and all DKB-specific exception types --
no DB access, no business logic.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.foundation.models.entities import now_utc

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Community-known FinTS product_id that is publicly shared and confirmed
# to be activated on DKB's side. Used ONLY for an explicit, user-initiated
# connectivity self-test -- never silently borrowed for production sync.
DKB_PUBLIC_TEST_PRODUCT_ID = "6151256F3D4F9975B877BD4A2"

# Signals from the decoupled TAN poll that mean "pending, not yet approved" (not a rejection).
# Only extend with exact substrings confirmed in Step 1 operator action.
_PENDING_TAN_SIGNALS = ("Bad status code 400",)

# Evict in-memory sessions older than 1 hour
_SESSION_TTL_SECONDS = 3600

# ---------------------------------------------------------------------------
# State type
# ---------------------------------------------------------------------------

SyncState = Literal[
    "pending_tan",
    "waiting_for_push",
    "needs_manual_tan",
    "confirmed",
    "failed",
    "cached",
    "expired",
]

# ---------------------------------------------------------------------------
# Sync session
# ---------------------------------------------------------------------------


@dataclass
class SyncSession:
    session_id: str
    state: SyncState
    message: str
    provider: str = "fints"
    challenge: str | None = None
    challenge_html: str | None = None
    decoupled: bool = False
    available_tan_methods: list[dict[str, str]] = field(default_factory=list)
    next_poll_after_seconds: int | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)
    logs: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DkbFinTSTanTimeout(RuntimeError):
    """Raised when the decoupled push TAN is not confirmed within the timeout window."""
    pass


class DkbFinTSConnectionRejected(RuntimeError):
    """Raised when DKB rejects the FinTS dialog at the HTTP layer -- usually
    due to wrong credentials or TAN method issues, but can also occur mid-dialog
    if the connection fails for accounts/balances/transactions segments.
    DKB does not validate product_id -- the built-in default always works."""

    def __init__(self, detail: str | None = None) -> None:
        msg = detail or (
            "DKB rejected the FinTS connection (HTTP 400 / could not establish system_id). "
            "Check that username and PIN are correct, and that BLZ is 12030000. "
            "If you received the DKB-App push notification the credentials are accepted -- "
            "the rejection is at a later segment (accounts/balances), which is unusual."
        )
        super().__init__(msg)


class DkbFinTSManualTanRequired(RuntimeError):
    def __init__(self, methods: list[dict[str, str]] | None = None, message: str | None = None) -> None:
        self.methods = methods or []
        if message is None:
            available = ", ".join(m["name"] for m in self.methods) if self.methods else "(none returned)"
            message = (
                "DKB-App decoupled push TAN is not available. "
                f"Available TAN methods: {available}. "
                "Enable DKB-App under Sicherheit > TAN-Verfahren in DKB online banking and retry."
            )
        super().__init__(message)

    @property
    def detail(self) -> dict[str, Any]:
        """Structured detail payload for API response surfaces."""
        return {
            "error": str(self),
            "available_tan_methods": self.methods,
            "fix": "Enable DKB-App under Sicherheit > TAN-Verfahren in DKB online banking.",
        }
