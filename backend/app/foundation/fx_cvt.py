"""FX live spot-rate resolution with in-memory TTL cache.

Dated conversion (``as_of``) lives one layer up in :mod:`app.foundation.fx_rates`
(``convert`` + ``FxRateProvider``), which builds on this module's ``get_rate``.
"""

from __future__ import annotations

import time
from typing import Any

from app.foundation.providers.registry import build_provider_registry

_CACHE_TTL = 900  # 15 minutes
_rate_cache: dict[tuple[str, str], tuple[float, float]] = {}


class FxRateUnavailable(RuntimeError):
    """Raised by convert(..., strict=True) when no FX rate can be resolved."""

    def __init__(self, from_currency: str, to_currency: str) -> None:
        self.from_currency = from_currency
        self.to_currency = to_currency
        super().__init__(f"No FX rate available for {from_currency}->{to_currency}")


def _cache_clear() -> None:
    """Clear the rate cache (for testing)."""
    _rate_cache.clear()


def get_rate(base: str, quote: str, db: Any) -> float | None:
    """Get FX rate from base to quote currency. Returns None on failure."""
    base = base.upper()
    quote = quote.upper()
    if base == quote:
        return 1.0

    cache_key = (base, quote)
    now = time.time()
    if cache_key in _rate_cache:
        rate, ts = _rate_cache[cache_key]
        if now - ts < _CACHE_TTL:
            return rate

    try:
        registry = build_provider_registry(db)
        result = registry.get_fx_rate(base, quote)
        if result.get("ok") and result.get("data"):
            rate = float(result["data"]["rate"])
            _rate_cache[cache_key] = (rate, now)
            return rate
    except Exception:
        pass

    return None
