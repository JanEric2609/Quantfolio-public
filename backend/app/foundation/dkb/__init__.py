"""DKB FinTS integration package.

Split from the monolithic ``app/services/dkb.py`` into four modules:

- ``utils`` -- Pure helper functions (encoding safety, parsing, log capture, URL safety)
- ``models`` -- Data classes, exception types, and constants
- ``adapter`` -- ``DkbFinTSAdapter`` (FinTS protocol, TAN lifecycle, account reads)
- ``service`` -- ``DkbSyncService`` (sync orchestration, persistence, daemon threads)

All public symbols from the original ``app.foundation.dkb`` module are re-exported
here so existing ``from app.foundation.dkb import ...`` imports continue to work
without changes.
"""

# ── Re-exports: models ──────────────────────────────────────────────
from app.foundation.dkb.models import (
    DKB_PUBLIC_TEST_PRODUCT_ID,
    DkbFinTSConnectionRejected,
    DkbFinTSManualTanRequired,
    DkbFinTSTanTimeout,
    SyncSession,
    SyncState,
)

# ── Re-exports: utils ───────────────────────────────────────────────
from app.foundation.dkb.utils import (
    _ascii_safe,
    _as_dict,
    _capture_dkb_logs,
    _coerce_int,
    _date_from_any,
    _decimal_from_any,
    _decimal_or_none,
    _first,
    _list_from_any,
    _safe_fints_url,
    _sanitize_fints_error,
    dkb_transaction_hash,
)

# ── Re-exports: adapter ─────────────────────────────────────────────
from app.foundation.dkb.adapter import DkbFinTSAdapter

# ── Re-exports: service ─────────────────────────────────────────────
from app.foundation.dkb.service import DkbSyncService, dkb_sync_service

# ── Public API ──────────────────────────────────────────────────────
__all__ = [
    # Models
    "DKB_PUBLIC_TEST_PRODUCT_ID",
    "DkbFinTSConnectionRejected",
    "DkbFinTSManualTanRequired",
    "DkbFinTSTanTimeout",
    "SyncSession",
    "SyncState",
    # Utils
    "_ascii_safe",
    "_as_dict",
    "_capture_dkb_logs",
    "_coerce_int",
    "_date_from_any",
    "_decimal_from_any",
    "_decimal_or_none",
    "_first",
    "_list_from_any",
    "_safe_fints_url",
    "_sanitize_fints_error",
    "dkb_transaction_hash",
    # Adapter
    "DkbFinTSAdapter",
    # Service
    "DkbSyncService",
    "dkb_sync_service",
]
