from __future__ import annotations

from abc import ABC
from datetime import datetime
from typing import Any


# Failure reasons a provider (or the connection probe) can report.
PROBE_REASONS = ("not_applicable", "rate_limited", "auth", "network", "http_error", "no_data")


def provider_result(
    provider: str,
    *,
    ok: bool,
    data: Any = None,
    stale: bool = False,
    as_of: datetime | None = None,
    missing_fields: list[str] | None = None,
    confidence: float = 0.0,
    warnings: list[str] | None = None,
    error: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "provider": provider,
        "data": data,
        "quality": {
            "stale": stale,
            "as_of": as_of,
            "missing_fields": missing_fields or [],
            "confidence": max(0.0, min(1.0, confidence)),
            "warnings": warnings or [],
        },
        "error": error,
        # Machine-readable outcome code of a failure (see PROBE_REASONS), None when unclassified.
        "reason": reason,
    }


class MarketDataProvider(ABC):
    name = "base"

    # Set of capability method names this provider supports.
    # Override in subclasses to enable first_success() capability filtering.
    capabilities: set[str] = set()

    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "enabled": self.enabled,
            "available": self.enabled,
            "message": "Provider is enabled.",
        }

    def unavailable(self, capability: str, message: str | None = None, *, reason: str | None = None) -> dict[str, Any]:
        return provider_result(
            self.name,
            ok=False,
            warnings=[message or f"{self.name} does not support {capability}."],
            error=message or f"{capability} unavailable",
            reason=reason,
        )

    def get_quote(self, symbol: str) -> dict[str, Any]:
        return self.unavailable("quote")

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        return self.unavailable("history")

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        return self.unavailable("fundamentals")

    def get_news(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        return self.unavailable("news")

    def get_analyst_estimates(self, symbol: str) -> dict[str, Any]:
        return self.unavailable("analyst estimates")

    def get_etf_holdings(self, symbol_or_isin: str) -> dict[str, Any]:
        return self.unavailable("ETF holdings")

    def get_etf_info(self, symbol_or_isin: str) -> dict[str, Any]:
        return self.unavailable("ETF info")

    def get_fx_rate(self, base: str, quote: str) -> dict[str, Any]:
        return self.unavailable("FX rate")

    def get_macro_indicators(self) -> dict[str, Any]:
        return self.unavailable("macro indicators")
