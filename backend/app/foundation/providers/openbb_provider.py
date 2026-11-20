from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.utils import is_us_listing, strip_exchange_suffix

logger = logging.getLogger(__name__)

_DEFAULT_CONFIDENCE = 0.72

# yfinance requires the original Yahoo Finance symbology including exchange
# suffixes (e.g. EUNL.DE). Every other ODP provider — news or otherwise —
# expects bare, US-style ticker symbols and has no reliable way to route a
# non-US listing, so a non-US symbol is skipped for those rather than
# stripped-and-sent: stripping risks matching an unrelated US ticker
# (SHEL.AS → SHEL) and returning the wrong company's data.
_SUFFIX_AWARE_PROVIDERS: set[str] = {"yfinance"}

# Candidate providers probed by probe_providers() to discover which are available.
# These cover the most commonly installed free extensions.
# Providers probed by probe_providers() to discover which are available.
# Expanded to cover all commonly installed free OpenBB extensions.
_PROBE_CANDIDATE_PROVIDERS = [
    # Zero-key providers (always probe — they work out of the box)
    "cboe", "finviz", "yfinance", "sec", "famafrench",
    "ecb", "imf", "federal_reserve", "oecd", "seeking_alpha", "tmx",
    # Free-key providers (probe only — fail gracefully if keys not configured)
    "fmp", "polygon", "tiingo", "fred", "tradier", "alpha_vantage", "intrinio",
]


def _default_result_kwargs() -> dict[str, Any]:
    return {"as_of": datetime.now(UTC), "confidence": _DEFAULT_CONFIDENCE}


def _normalize_quote(data: list[Any] | Any) -> list[Any] | Any:
    """Normalize quote results: ensure ``close`` is present (alias from ``last_price``).

    OpenBB returns ``last_price`` for equity quotes; downstream code reads ``close``.
    We add ``close`` when it is absent so callers do not need to know the OBBject
    field name.  The original ``last_price`` key is left untouched.
    """
    if not isinstance(data, list):
        return data
    normalized: list[Any] = []
    for item in data:
        if isinstance(item, dict) and "close" not in item and "last_price" in item:
            item = {**item, "close": item["last_price"]}
        normalized.append(item)
    return normalized


def _normalize_history(data: list[Any] | Any) -> list[Any] | Any:
    """Normalize history rows: ensure ``date`` and ``close`` keys are present.

    OpenBB historical rows may use ``datetime`` instead of ``date``, and
    ``close`` is generally present but ``open``/``high``/``low`` may vary.
    We only ensure the two fields downstream code strictly requires.
    """
    if not isinstance(data, list):
        return data
    normalized: list[Any] = []
    for item in data:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        row = dict(item)
        # Promote "datetime" → "date" when "date" absent
        if "date" not in row and "datetime" in row:
            row["date"] = row["datetime"]
        # Derive "close" from "last_price" as a last resort (rare for historical)
        if "close" not in row and "last_price" in row:
            row["close"] = row["last_price"]
        normalized.append(row)
    return normalized


def _normalize_analyst_estimates(data: list[Any] | Any) -> dict[str, Any] | Any:
    """Unwrap the single-symbol consensus response to a bare dict.

    OpenBB's ``results`` envelope is always a list (even for one symbol) —
    callers (``pipeline.py:stage_sentiment_fundamentals``) expect a dict with
    ``.get("target_high")`` etc.; calling ``.get`` on the raw list raises
    AttributeError, silently swallowed upstream, pinning the score neutral.
    """
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return data


_FUNDAMENTAL_KEY_MAP = {
    "price_to_earnings": "pe_ratio",
    "price_earnings_ratio": "pe_ratio",
    "pe": "pe_ratio",
    "price_to_book": "pb_ratio",
    "price_book_ratio": "pb_ratio",
    "pb": "pb_ratio",
    "return_on_equity": "roe",
    "return_on_invested_capital": "roic",
    "debt_to_equity": "debt_equity",
    "total_debt_to_equity": "debt_equity",
    "market_capitalization": "market_cap",
    "net_profit_margin": "profit_margin",
    "price_to_sales": "ps_ratio",
    "div_yield": "dividend_yield",
    "earnings_growth": "earnings_growth",
    "eps_growth": "earnings_growth",
}


def _normalize_fundamentals(data: list[Any] | Any) -> dict[str, Any] | Any:
    """Unwrap and normalize OpenBB fundamentals response to a canonical dictionary.

    OpenBB's ``/api/v1/equity/fundamental/metrics`` returns ``results`` as a list of
    period rows with varied provider field names. This normalizes both the structure
    and key aliases.
    """
    raw_dict: dict[str, Any]
    if isinstance(data, list):
        if not data or not isinstance(data[0], dict):
            return {}
        raw_dict = dict(data[0])
    elif isinstance(data, dict):
        raw_dict = dict(data)
    else:
        return {}

    normalized: dict[str, Any] = {}
    for k, v in raw_dict.items():
        canonical_k = _FUNDAMENTAL_KEY_MAP.get(k, k)
        normalized[canonical_k] = v
        # Also ensure dual availability of debt_equity and debt_to_equity for downstream callers
        if canonical_k == "debt_equity":
            normalized["debt_to_equity"] = v

    return normalized


# Capability-level normalizers applied after OBBject unwrap.
_NORMALIZERS: dict[str, Any] = {
    "quote": _normalize_quote,
    "history": _normalize_history,
    "analyst_estimates": _normalize_analyst_estimates,
    "fundamentals": _normalize_fundamentals,
}


class OpenBBProvider(MarketDataProvider):
    name = "openbb"
    capabilities = {"get_quote", "get_history", "get_fundamentals", "get_news",
                    "get_analyst_estimates", "get_etf_holdings", "get_etf_info",
                    "get_fx_rate", "get_macro_indicators", "get_fred_series"}

    def __init__(
        self,
        *,
        enabled: bool,
        api_url: str = "http://127.0.0.1:6900",
        news_providers: list[str] | None = None,
        default_provider: str = "yfinance",
    ):
        super().__init__(enabled=enabled)
        self.api_url = api_url.rstrip("/")
        # Default to yfinance-only when no list is explicitly provided (safest default).
        self._news_providers: list[str] = news_providers if news_providers is not None else ["yfinance"]
        # ODP default provider injected into all non-news requests when not already specified.
        self._default_provider = default_provider

    def status(self) -> dict[str, Any]:
        api_available = False
        if self.enabled:
            try:
                with httpx.Client(timeout=2) as client:
                    response = client.get(f"{self.api_url}/")
                    api_available = response.status_code < 500
            except Exception:
                api_available = False
        return {
            "provider": self.name,
            "enabled": self.enabled,
            "available": self.enabled and api_available,
            "api_url": self.api_url,
            "default_provider": self._default_provider,
            "news_providers": list(self._news_providers),
            "message": (
                "OpenBB API is available."
                if self.enabled and api_available
                else "OpenBB is optional and currently unavailable or disabled."
            ),
        }

    def probe_providers(
        self,
        symbol: str = "AAPL",
        candidates: list[str] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Probe candidate ODP providers with a sample equity quote request.

        Tries each provider in *candidates* (defaults to ``_PROBE_CANDIDATE_PROVIDERS``)
        against the live ODP and reports which ones respond successfully.

        Returns a dict with:
        - ``available``: list of provider names that returned HTTP 200
        - ``unavailable``: list of provider names that failed
        - ``errors``: mapping of provider name → error string for failures

        Signature: ``probe_providers(symbol="AAPL", candidates=None, timeout=5.0) -> dict``
        """
        if not self.enabled:
            return {"available": [], "unavailable": [], "errors": {"*": "OpenBB is disabled."}}

        if candidates is None:
            candidates = list(_PROBE_CANDIDATE_PROVIDERS)

        endpoint = f"{self.api_url}/api/v1/equity/price/quote"
        available: list[str] = []
        unavailable: list[str] = []
        errors: dict[str, str] = {}

        for provider_name in candidates:
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.get(endpoint, params={"symbol": symbol, "provider": provider_name})
                if resp.status_code == 200:
                    available.append(provider_name)
                else:
                    unavailable.append(provider_name)
                    errors[provider_name] = f"HTTP {resp.status_code}"
            except Exception as exc:
                unavailable.append(provider_name)
                errors[provider_name] = str(exc)

        logger.info(
            "probe_providers symbol=%s available=%s unavailable=%s",
            symbol, available, unavailable,
        )
        return {"available": available, "unavailable": unavailable, "errors": errors}

    def get_quote(self, symbol: str) -> dict[str, Any]:
        return self._request("quote", symbol=symbol)

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        return self._request("history", symbol=symbol, start=start, end=end, days=days)

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        return self._request("fundamentals", symbol=symbol)

    def get_news(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        return self._request("news", symbol=symbol, limit=limit)

    def get_analyst_estimates(self, symbol: str) -> dict[str, Any]:
        return self._request("analyst_estimates", symbol=symbol)

    def get_etf_holdings(self, symbol_or_isin: str) -> dict[str, Any]:
        return self._request("etf_holdings", symbol=symbol_or_isin)

    def get_etf_info(self, symbol_or_isin: str) -> dict[str, Any]:
        """ETF descriptive metadata (name, inception date, index tracked) —
        distinct from get_etf_holdings (constituents). ADR 0014 §6: neither
        this call nor its consumer existed before; added to ground the
        Discover dossier prompt in inception date / tracked index instead of
        leaving the LLM to guess them from a ticker."""
        return self._request("etf_info", symbol=symbol_or_isin)

    def get_fx_rate(self, base: str, quote: str) -> dict[str, Any]:
        return self._request("fx_rate", base=base, quote=quote)

    def get_macro_indicators(self) -> dict[str, Any]:
        return self._request("macro_indicators")

    def get_fred_series(
        self,
        series_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one FRED series' time-series values via OpenBB's fred extension.

        docs/adr/0010-openbb-per-purpose-routing.md: macro/rate data routed
        through OpenBB must go through its purpose-built fred/ecb sub-providers,
        never the generic ``default_provider`` (yfinance) — a plain economy
        call would otherwise silently get ``provider=yfinance`` injected by
        ``_api_request``'s default-provider fallback, which does not serve
        economic time series at all. ``provider="fred"`` is forced explicitly
        here rather than relying on that injection.

        Distinct from ``get_macro_indicators`` (kept as-is, a near-no-op
        against the economy/calendar *events* endpoint — see its own
        docstring) — this is the real per-series values path the audit asked
        for, additive rather than a replacement.
        """
        if not self.enabled:
            return provider_result(
                self.name,
                ok=False,
                warnings=["OpenBB is disabled in settings."],
                error="OpenBB disabled",
            )
        params: dict[str, Any] = {"symbol": series_id, "provider": "fred"}
        if start:
            params["start_date"] = start
        if end:
            params["end_date"] = end
        return self._api_request("fred_series", params)

    def _request(self, capability: str, **params: Any) -> dict[str, Any]:
        if not self.enabled:
            return provider_result(
                self.name,
                ok=False,
                warnings=["OpenBB is disabled in settings."],
                error="OpenBB disabled",
            )
        # For non-news capabilities strip the exchange suffix once here.
        # News symbology is handled per-provider inside _api_request.
        if capability != "news" and "symbol" in params and params["symbol"] is not None:
            symbol = params["symbol"]
            if self._default_provider not in _SUFFIX_AWARE_PROVIDERS:
                if not is_us_listing(symbol):
                    return provider_result(
                        self.name,
                        ok=False,
                        warnings=[
                            f"OpenBB default_provider={self._default_provider!r} does not "
                            f"support non-US symbol {symbol}; skipping to avoid a ticker "
                            "collision."
                        ],
                        error=f"non-US symbol unsupported by provider {self._default_provider}",
                    )
                stripped = strip_exchange_suffix(symbol)
                if stripped != symbol:
                    params = {**params, "symbol": stripped}
        return self._api_request(capability, params)

    # OpenBB REST API expects start_date/end_date, not start/end.
    _PARAM_RENAMES: dict[str, str] = {
        "start": "start_date",
        "end": "end_date",
    }

    def _api_request(self, capability: str, params: dict[str, Any]) -> dict[str, Any]:
        endpoint_map = {
            "quote": "/api/v1/equity/price/quote",
            "history": "/api/v1/equity/price/historical",
            "fundamentals": "/api/v1/equity/fundamental/metrics",
            "news": "/api/v1/news/company",
            # Real ODP router path is "estimates/consensus" (PriceTargetConsensus
            # data model: target_high/target_low/target_consensus/target_median/
            # recommendation/current_price) — "estimates/analyst" 404s, it isn't
            # a route OpenBB exposes. Verified live against the yfinance provider.
            "analyst_estimates": "/api/v1/equity/estimates/consensus",
            "etf_holdings": "/api/v1/etf/holdings",
            "etf_info": "/api/v1/etf/info",
            "fx_rate": "/api/v1/currency/price/historical",
            # NOTE: OpenBB's economy/indicators requires a `symbol` param we don't
            # have. The calendar endpoint returns economic *events* (CPI release dates,
            # Fed meetings, etc.) rather than time-series data. This is acceptable
            # because the provider registry chain will fall through to the FRED
            # provider which provides the actual macro indicator values. The
            # macro_indicators capability here is intentionally a near-no-op.
            "macro_indicators": "/api/v1/economy/calendar",
            # Real per-series FRED time-series values (obb.economy.fred_series
            # -> /api/v1/economy/fred_series), forced to provider=fred by
            # get_fred_series — the actual "macro/rates via OpenBB's fred
            # extension" path, distinct from the calendar near-no-op above.
            "fred_series": "/api/v1/economy/fred_series",
        }
        try:
            # --- days → start_date/end_date conversion (history capability) -------
            # OpenBB does not accept a bare ``days`` param; convert it to an explicit
            # date window before the general param-rename pass below.
            if "days" in params and params["days"] is not None:
                today = date.today()
                params = {
                    **params,
                    "start": (today - timedelta(days=int(params["days"]))).isoformat(),
                    "end": today.isoformat(),
                }
            # Always drop the raw ``days`` key — even when it was None — so it is
            # never forwarded to the ODP.
            params = {k: v for k, v in params.items() if k != "days"}

            # Rename internal param names to match OpenBB REST API expectations.
            api_params = {
                self._PARAM_RENAMES.get(k, k): v
                for k, v in params.items()
                if v is not None
            }

            if capability == "news":
                original_symbol = api_params.get("symbol")
                errors: list[str] = []
                for provider_name in self._news_providers:
                    # yfinance needs the original Yahoo Finance symbol (with suffix like .L).
                    # All other providers need the bare symbol without exchange suffix —
                    # and can't safely serve a non-US listing at all: stripping risks
                    # matching an unrelated US ticker (SHEL.AS → SHEL).
                    if provider_name in _SUFFIX_AWARE_PROVIDERS:
                        symbol_for_provider = original_symbol
                    elif original_symbol is not None:
                        if not is_us_listing(original_symbol):
                            errors.append(
                                f"{provider_name}: does not support non-US symbol {original_symbol}"
                            )
                            continue
                        symbol_for_provider = strip_exchange_suffix(original_symbol)
                    else:
                        symbol_for_provider = None

                    provider_params = {**api_params, "provider": provider_name}
                    if symbol_for_provider is not None:
                        provider_params["symbol"] = symbol_for_provider
                    try:
                        with httpx.Client(timeout=8) as client:
                            response = client.get(
                                f"{self.api_url}{endpoint_map[capability]}",
                                params=provider_params,
                            )
                            response.raise_for_status()
                            raw = response.json()
                        # Unwrap OBBject envelope: {"results": [...], "provider": ..., "warnings": [...]}
                        data = raw.get("results", raw) if isinstance(raw, dict) else raw
                        envelope_warnings = raw.get("warnings") if isinstance(raw, dict) else None
                        warn_list: list[str] = []
                        if envelope_warnings:
                            warn_list = [str(w) for w in envelope_warnings]
                        logger.debug("news fetched successfully via provider=%s", provider_name)
                        return provider_result(
                            self.name,
                            ok=True,
                            data=data,
                            warnings=warn_list or None,
                            **_default_result_kwargs(),
                        )
                    except Exception as exc:
                        errors.append(f"{provider_name}: {exc}")
                        logger.warning("news provider=%s failed: %s", provider_name, exc)
                return provider_result(
                    self.name,
                    ok=False,
                    warnings=[f"All news providers failed: {'; '.join(errors)}"],
                    error=f"No news provider succeeded (tried: {', '.join(self._news_providers)})",
                )

            # --- Inject default ODP provider for all non-news capabilities ----------
            # Only inject when the caller has not already specified a provider.
            if "provider" not in api_params:
                api_params["provider"] = self._default_provider

            with httpx.Client(timeout=8) as client:
                response = client.get(f"{self.api_url}{endpoint_map[capability]}", params=api_params)
                response.raise_for_status()
                raw = response.json()

            # --- Unwrap OBBject envelope -------------------------------------------
            # OpenBB wraps all responses as {"results": [...], "provider": ...,
            # "warnings": [...], "chart": null, "extra": {}}.
            # We extract "results" as the canonical data payload and surface any
            # envelope-level warnings into the provider result.
            if isinstance(raw, dict):
                data = raw.get("results", raw)
                envelope_warnings = raw.get("warnings") or []
            else:
                data = raw
                envelope_warnings = []

            # Apply per-capability field normalization so downstream code gets the
            # field names it expects (e.g. ``close``, ``date``).
            normalizer = _NORMALIZERS.get(capability)
            if normalizer is not None:
                data = normalizer(data)

            warn_list = [str(w) for w in envelope_warnings]
            return provider_result(
                self.name,
                ok=True,
                data=data,
                warnings=warn_list,
                **_default_result_kwargs(),
            )
        except Exception as exc:
            return provider_result(self.name, ok=False, warnings=[f"OpenBB API unavailable: {exc}"], error=str(exc))
