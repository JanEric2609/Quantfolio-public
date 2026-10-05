"""European Central Bank (ECB) Statistical Data Warehouse provider."""

from datetime import datetime
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result


class EcbProvider(MarketDataProvider):
    """ECB provider for euro-area macroeconomic indicators (HICP, interest rates, etc.)."""

    name = "ecb_sdw"
    capabilities = {"get_macro_indicators", "get_history"}
    base_url = "https://data-api.ecb.europa.eu"

    def __init__(self, *, enabled: bool = True):
        super().__init__(enabled=enabled)

    def status(self) -> dict[str, Any]:
        base = super().status()
        if self.enabled:
            base["available"] = True
            base["message"] = "ECB SDW provider is available (no API key required)."
        else:
            base["available"] = False
            base["message"] = "ECB SDW provider is disabled."
        return base

    def get_macro_indicators(self, series_ids: list[str] | None = None) -> dict[str, Any]:
        """Fetch macro indicators from ECB Statistical Data Warehouse.

        Args:
            series_ids: List of ECB series IDs (e.g., ['HICP', 'FM.Q.U2.EUR.4F.KR.MRR_RT']).
                        Defaults to HICP (inflation rate).
        """
        if not self.enabled:
            return self.unavailable(
                "macro indicators", "ECB SDW provider is disabled"
            )

        if series_ids is None:
            series_ids = [
                "ICP.M.U2.N.000000.4.ANR",  # HICP all items, euro area
                "FM.Q.U2.EUR.4F.KR.MRR_RT",  # Main refinancing rate
            ]

        try:
            data = {}
            for series_id in series_ids:
                try:
                    result = self._fetch_sdw_series(series_id)
                    if result.get("ok"):
                        data[series_id] = result.get("data")
                except Exception:
                    # Skip individual series errors
                    pass

            if not data:
                return provider_result(
                    self.name,
                    ok=False,
                    error="No data retrieved from ECB SDW",
                )

            return provider_result(
                self.name,
                ok=True,
                data=data,
                as_of=datetime.now(),
                confidence=0.90,
            )

        except Exception as e:
            return provider_result(
                self.name,
                ok=False,
                error=str(e),
                warnings=[f"ECB SDW fetch failed: {str(e)}"],
            )

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """Fetch historical data from ECB SDW.

        Args:
            symbol: ECB series ID (e.g., 'ICP.M.U2.N.000000.4.ANR' for HICP).
            start: ISO format start date.
            end: ISO format end date.
        """
        if not self.enabled:
            return self.unavailable("history", "ECB SDW provider is disabled")

        try:
            result = self._fetch_sdw_series(symbol, start_date=start, end_date=end)
            if result.get("ok"):
                return provider_result(
                    self.name,
                    ok=True,
                    data=result.get("data"),
                    as_of=datetime.now(),
                    confidence=0.90,
                )
            else:
                return provider_result(
                    self.name,
                    ok=False,
                    error=f"ECB SDW fetch failed for {symbol}",
                )

        except Exception as e:
            return provider_result(
                self.name,
                ok=False,
                error=str(e),
                warnings=[f"ECB SDW history fetch failed: {str(e)}"],
            )

    def _fetch_sdw_series(
        self,
        series_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a single series from ECB Data Portal API.

        The ECB Data Portal API uses a SDMX REST interface. Series format:
        {flowRef}/{key}  e.g. ICP/M.U2.N.000000.4.ANR
        The full series_id passed in is expected as "FLOW.key" dot-notation,
        where the first segment is the flowRef and the rest form the key.
        Examples:
          ICP.M.U2.N.000000.4.ANR  -> flowRef=ICP, key=M.U2.N.000000.4.ANR
          FM.Q.U2.EUR.4F.KR.MRR_RT -> flowRef=FM, key=Q.U2.EUR.4F.KR.MRR_RT
        """
        try:
            # Split the series_id into flowRef (first segment) and key (rest).
            parts = series_id.split(".", 1)
            if len(parts) == 2:
                flow_ref, key = parts
            else:
                # If only one part, treat whole string as the key under flowRef "DATA"
                flow_ref = "DATA"
                key = series_id

            # ECB Data Portal REST API (replaces retired sdw-wsrest.ecb.europa.eu)
            url = f"https://data-api.ecb.europa.eu/service/data/{flow_ref}/{key}"

            params: dict[str, str] = {"format": "jsondata"}
            if start_date:
                params["startPeriod"] = start_date
            if end_date:
                params["endPeriod"] = end_date

            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()

            json_data = resp.json()

            # Parse SDMX-JSON response structure. Observations are nested two
            # levels below dataSets[0]: under "series", keyed by series key
            # (e.g. "0:0:0:0:0"), each with its own "observations" map keyed
            # by observation index (e.g. "0", "1", ...) -> [value, status, ...].
            observations = []
            if "dataSets" in json_data and len(json_data["dataSets"]) > 0:
                dataset = json_data["dataSets"][0]
                for series in dataset.get("series", {}).values():
                    for obs_key, obs_values in series.get("observations", {}).items():
                        if isinstance(obs_values, list) and len(obs_values) > 0:
                            observations.append({
                                "key": obs_key,
                                "value": obs_values[0],
                            })

            return {
                "ok": len(observations) > 0,
                "data": observations,
            }

        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
            }
