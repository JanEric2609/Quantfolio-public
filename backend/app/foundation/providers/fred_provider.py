"""FRED (Federal Reserve Economic Data) provider for macro indicators."""

from datetime import datetime
from typing import Any, cast

import pandas as pd

from app.foundation.providers.base import MarketDataProvider, provider_result


class FredProvider(MarketDataProvider):
    """FRED provider for macroeconomic indicators via fredapi."""

    name = "fred"
    capabilities = {"get_macro_indicators", "get_history"}

    def __init__(self, api_key: str | None = None, *, enabled: bool = True):
        super().__init__(enabled=enabled)
        self.api_key = api_key
        self._fred = None
        if api_key and enabled:
            try:
                import fredapi

                self._fred = fredapi.Fred(api_key=api_key)
            except Exception as e:
                self.enabled = False
                self._error = str(e)

    def status(self) -> dict[str, Any]:
        base = super().status()
        if self.enabled and self._fred:
            try:
                # Test with a common series
                self._fred.get_series("GDP", observation_start="2024-01-01", observation_end="2024-01-02")
                base["available"] = True
                base["message"] = "FRED API is available."
            except Exception as e:
                base["available"] = False
                base["message"] = f"FRED API error: {str(e)}"
        elif not self.enabled:
            base["available"] = False
            base["message"] = "FRED provider is disabled or API key is missing."
        return base

    def get_macro_indicators(self, series_ids: list[str] | None = None) -> dict[str, Any]:
        """Fetch macro indicators from FRED.

        Args:
            series_ids: List of FRED series IDs (e.g., ['GDP', 'UNRATE', 'DGS10']).
                        Defaults to common indicators: VIX, treasury yields, CPI.
        """
        if not self.enabled or not self._fred:
            return self.unavailable("macro indicators", "FRED provider not available")

        if series_ids is None:
            series_ids = [
                "VIXCLS",  # VIX
                "DGS2",    # 2-year treasury
                "DGS10",   # 10-year treasury
                "DCOILWTICO",  # Oil prices
                "CPIAUCSL",    # CPI all items
            ]

        try:
            data = {}
            for series_id in series_ids:
                try:
                    series = self._fred.get_series(series_id)
                    if len(series) > 0:
                        data[series_id] = {
                            "value": float(series.iloc[-1]),
                            "timestamp": cast(pd.Timestamp, series.index[-1]).isoformat(),
                        }
                except Exception:
                    # Skip individual series errors
                    pass

            if not data:
                return provider_result(
                    self.name,
                    ok=False,
                    error="No data retrieved from FRED",
                )

            return provider_result(
                self.name,
                ok=True,
                data=data,
                as_of=datetime.now(),
                confidence=0.95,
            )

        except Exception as e:
            return provider_result(
                self.name,
                ok=False,
                error=str(e),
                warnings=[f"FRED fetch failed: {str(e)}"],
            )

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """FRED does not support single-symbol quotes."""
        return self.unavailable("quote")

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """Fetch historical series data from FRED.

        Args:
            symbol: FRED series ID (e.g., 'DGS10' for 10-year treasury yield).
            start: ISO format start date.
            end: ISO format end date.
            days: Number of days to fetch (if start/end not provided).
        """
        if not self.enabled or not self._fred:
            return self.unavailable("history", "FRED provider not available")

        try:
            series = self._fred.get_series(symbol, observation_start=start, observation_end=end)

            if series is None or len(series) == 0:
                return provider_result(
                    self.name,
                    ok=False,
                    error=f"No data found for series {symbol}",
                )

            # Convert to list of dicts
            data = [
                {"date": cast(pd.Timestamp, idx).isoformat(), "value": float(val)}
                for idx, val in series.items()
            ]

            return provider_result(
                self.name,
                ok=True,
                data=data,
                as_of=datetime.now(),
                confidence=0.95,
            )

        except Exception as e:
            return provider_result(
                self.name,
                ok=False,
                error=str(e),
                warnings=[f"FRED history fetch failed: {str(e)}"],
            )
