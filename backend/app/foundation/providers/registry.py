from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.foundation.core.config import get_settings
from app.foundation.providers.alpaca_provider import AlpacaProvider
from app.foundation.providers.alphavantage_provider import AlphaVantageProvider
from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.databento_provider import DatabentoProvider
from app.foundation.providers.ecb_provider import EcbProvider
from app.foundation.providers.eod_provider import EodProvider
from app.foundation.providers.finnhub_provider import FinnhubProvider
from app.foundation.providers.fred_provider import FredProvider
from app.foundation.providers.justetf_provider import JustETFProvider
from app.foundation.providers.massive_provider import MassiveProvider
from app.foundation.providers.openbb_provider import OpenBBProvider
from app.foundation.providers.tiingo_provider import TiingoProvider
from app.foundation.providers.twelvedata_provider import TwelveDataProvider
from app.foundation.providers.rate_limiter import acquire_all, create_limiters
from app.foundation.providers.utils import is_us_listing
from app.foundation.providers.yfinance_provider import YFinanceProvider
from app.foundation.settings import get_public_settings, get_secret, resolve_openbb_default_provider


# Cache TTL for the provider registry (seconds)
_REGISTRY_CACHE_TTL = 300  # 5 minutes
_registry_cache: tuple[float, ProviderRegistry] | None = None

_PER_PROVIDER_TIMEOUT_S = float(os.getenv("PER_PROVIDER_TIMEOUT_S") or "30")


_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def _is_us_symbol(symbol: str) -> bool:
    """False for an exchange-suffixed ticker (EUNL.DE) or a non-US ISIN."""
    candidate = symbol.strip().upper()
    if _ISIN_RE.match(candidate):
        return candidate.startswith("US")
    return is_us_listing(candidate)


class ProviderRegistry:
    _DEAD_COOLDOWN = 300  # seconds — matches the 300s registry cache TTL

    def __init__(self, providers: list[MarketDataProvider]):
        self.providers = providers
        # Per-capability map of provider names that have failed and should be skipped,
        # mapped to the monotonic timestamp at which the cooldown expires.
        # Key: capability method name (e.g. "get_quote")
        # Value: dict of provider_name -> monotonic deadline
        self._dead_providers: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def status(self) -> list[dict[str, Any]]:
        return [provider.status() for provider in self.providers]

    def _mark_dead(self, capability: str, provider_name: str) -> None:
        """Mark a provider as dead for a specific capability (cooldown TTL)."""
        with self._lock:
            dead_until = time.monotonic() + self._DEAD_COOLDOWN
            self._dead_providers.setdefault(capability, {})[provider_name] = dead_until

    def _is_dead(self, capability: str, provider_name: str) -> bool:
        """Check if a provider is currently in cooldown for a capability."""
        with self._lock:
            dead_until = self._dead_providers.get(capability, {}).get(provider_name)
            if dead_until is None:
                return False
            if time.monotonic() >= dead_until:
                # Cooldown expired — remove entry lazily
                self._dead_providers.get(capability, {}).pop(provider_name, None)
                return False
            return True

    def _revive(self, capability: str, provider_name: str) -> None:
        """Remove a provider from the dead list for a capability (it succeeded)."""
        with self._lock:
            cap_dead = self._dead_providers.get(capability)
            if cap_dead and provider_name in cap_dead:
                cap_dead.pop(provider_name, None)

    def first_success(
        self,
        capability: str,
        *args: Any,
        provider_timeout_s: float | None = None,
        providers: list[MarketDataProvider] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Try *capability* against providers in order, returning the first success.

        ``providers`` overrides the registry's own ``provider_chain_json``
        order for this one call — used by :meth:`get_price_history` (ADR 0014
        §7) to force a purpose-specific order without touching the general
        chain every other capability still uses.
        """
        timeout = provider_timeout_s if provider_timeout_s is not None else _PER_PROVIDER_TIMEOUT_S
        attempted = []
        for provider in (providers if providers is not None else self.providers):
            if not provider.enabled:
                attempted.append(provider.status().get("message", f"{provider.name} disabled"))
                continue
            # Capability filtering: skip if provider doesn't support this capability
            if capability not in provider.capabilities:
                attempted.append(f"{provider.name} does not support {capability}")
                continue
            # Adaptive skipping: skip if provider is marked dead for this capability
            if self._is_dead(capability, provider.name):
                attempted.append(f"{provider.name}: skipping (previously failed for {capability})")
                continue
            try:
                handler: Callable[..., dict[str, Any]] = getattr(provider, capability)
            except AttributeError:
                attempted.append(f"{provider.name}: missing method {capability}")
                continue

            # Rate-limit check: skip provider if any window is full
            limiters = create_limiters(provider.name)
            if limiters and not acquire_all(limiters):
                attempted.append(f"{provider.name}: throttled")
                continue

            executor = ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(handler, *args, **kwargs)
                result = future.result(timeout=timeout)
            except FutureTimeoutError:
                self._mark_dead(capability, provider.name)
                attempted.append(f"{provider.name}: timed out after {timeout:.0f}s")
                continue
            except Exception as exc:
                self._mark_dead(capability, provider.name)
                attempted.append(f"{provider.name}: {exc}")
                continue
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            if result.get("ok"):
                # Guarantee that fundamentals data payload is always a dict
                if capability == "get_fundamentals" and isinstance(result.get("data"), list):
                    raw_list = result["data"]
                    result["data"] = raw_list[0] if raw_list and isinstance(raw_list[0], dict) else {}
                self._revive(capability, provider.name)
                return result
            # Provider returned ok=False (e.g. symbol not found). Do NOT mark dead.
            attempted.extend(result.get("quality", {}).get("warnings", []))
            if result.get("error"):
                attempted.append(f"{provider.name}: {result['error']}")
        return provider_result(
            "registry",
            ok=False,
            warnings=attempted or ["No provider returned data."],
            error="; ".join(attempted) if attempted else "No provider returned data.",
        )

    def get_quote(self, symbol: str) -> dict[str, Any]:
        return self.first_success("get_quote", symbol)

    def get_history(self, symbol: str, start: str | None = None, end: str | None = None, days: int | None = None) -> dict[str, Any]:
        return self.first_success("get_history", symbol, start=start, end=end, days=days)

    # Explicit provider order for bar_prices ingestion (ADR 0014 §7) — quant
    # grade sources known for clean, adjusted OHLCV first, generic reference
    # sources (finnhub, openbb-via-yfinance, yfinance itself) last-resort only.
    # Distinct from the general provider_chain_json order get_history() uses,
    # which deliberately prefers yfinance for quotes/fundamentals (ADR 0010) —
    # that preference is wrong for the price-history writer specifically.
    _PRICE_HISTORY_PROVIDER_ORDER = ["tiingo", "twelvedata", "databento"]
    # Providers that only know US listings: asking them for a European symbol
    # wastes a call (and a rate-limit slot) per symbol.
    _US_ONLY_HISTORY_PROVIDERS = frozenset({"tiingo", "databento", "alpaca", "finnhub", "alphavantage"})
    # EODHD's free plan is 20 calls a day, burned within a minute by the
    # ingestion universe: never part of automatic ingestion, for any symbol.
    # Use it only by asking for it: get_price_history(..., provider="eod").
    _EXPLICIT_ONLY_HISTORY_PROVIDERS = frozenset({"eod"})

    def get_price_history(
        self, symbol: str, start: str | None = None, end: str | None = None, days: int | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Price history for bar_prices ingestion, routed to quant-grade sources first.

        Falls back to the general provider_chain_json order (which still
        includes yfinance) only if none of the quant-grade providers succeed —
        DataIngester.ingest_bar_prices is the only intended caller. Every
        other price-history reader (quotes, dossiers, etc.) should keep using
        get_history(), where yfinance is deliberately preferred.
        """
        by_name = {p.name: p for p in self.providers}
        if provider is not None:
            chosen = [by_name[provider]] if provider in by_name else []
            return self.first_success("get_history", symbol, start=start, end=end, days=days, providers=chosen)
        skip: set[str] = set(self._EXPLICIT_ONLY_HISTORY_PROVIDERS)
        if not _is_us_symbol(symbol):
            skip |= self._US_ONLY_HISTORY_PROVIDERS
        ordered = [by_name[name] for name in self._PRICE_HISTORY_PROVIDER_ORDER if name in by_name and name not in skip]
        ordered += [
            p for p in self.providers if p.name not in self._PRICE_HISTORY_PROVIDER_ORDER and p.name not in skip
        ]
        return self.first_success("get_history", symbol, start=start, end=end, days=days, providers=ordered)

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        return self.first_success("get_fundamentals", symbol)

    def get_news(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        return self.first_success("get_news", symbol, limit=limit)

    # Explicit provider order for analyst consensus. yfinance returns price
    # targets for US and non-US listings alike; OpenBB serves the same Yahoo
    # data behind a 30/min guardrail; Finnhub's free tier gets HTTP 403 on
    # /stock/price-target and only yields a buy/hold/sell label for US names.
    # Under the general chain Finnhub answered first for US listings (label
    # only, no target) and OpenBB throttled about half of a Discover run's
    # non-US listings (BBVA.MC audit, 2026-09-26).
    _ANALYST_PROVIDER_ORDER = ["yfinance", "openbb", "finnhub"]

    def get_analyst_estimates(self, symbol: str) -> dict[str, Any]:
        by_name = {p.name: p for p in self.providers}
        ordered = [by_name[name] for name in self._ANALYST_PROVIDER_ORDER if name in by_name]
        ordered += [p for p in self.providers if p.name not in self._ANALYST_PROVIDER_ORDER]
        return self.first_success("get_analyst_estimates", symbol, providers=ordered)

    # Yahoo first: it covers European listings, which Finnhub's free
    # earnings calendar refuses with HTTP 403.
    _EARNINGS_PROVIDER_ORDER = ["yfinance", "finnhub"]

    def get_earnings_calendar(self, symbol: str) -> dict[str, Any]:
        by_name = {p.name: p for p in self.providers}
        ordered = [by_name[name] for name in self._EARNINGS_PROVIDER_ORDER if name in by_name]
        return self.first_success("get_earnings_calendar", symbol, providers=ordered)

    def get_etf_holdings(self, symbol_or_isin: str) -> dict[str, Any]:
        return self.first_success("get_etf_holdings", symbol_or_isin)

    def get_etf_info(self, symbol_or_isin: str) -> dict[str, Any]:
        """ETF descriptive metadata (name, inception date, tracked index) —
        ADR 0014 §6. Only OpenBBProvider declares this capability today."""
        return self.first_success("get_etf_info", symbol_or_isin)

    def get_fx_rate(self, base: str, quote: str) -> dict[str, Any]:
        return self.first_success("get_fx_rate", base, quote)

    def get_dividends(self, symbol: str, start: str) -> dict[str, Any]:
        return self.first_success("get_dividends", symbol, start)

    def get_macro_indicators(self) -> dict[str, Any]:
        return self.first_success("get_macro_indicators")

    def get_fred_series(
        self, series_id: str, start: str | None = None, end: str | None = None,
    ) -> dict[str, Any]:
        """FRED series time-series values, routed via OpenBB's fred extension
        when enabled (docs/adr/0010-openbb-per-purpose-routing.md). Only
        ``OpenBBProvider`` declares this capability today — a direct
        ``FredProvider`` call (``services/providers/fred_provider.py``) stays
        the non-OpenBB path for callers that want it, deliberately not folded
        into this chain dispatch (see ``DataIngester.ingest_macro_indicators``'s
        own reasoning for isolating FRED-coded series from generic fallover)."""
        return self.first_success("get_fred_series", series_id, start=start, end=end)


# Fallback for an invalid/unparseable provider_chain_json override. Must stay
# in sync with DEFAULT_PUBLIC_SETTINGS["provider_chain_json"] (settings.py).
# yfinance sits ahead of openbb (docs/adr/0010-openbb-per-purpose-routing.md):
# quote/history/fundamentals traffic must keep resolving via yfinance first
# even once openbb_enabled unlocks openbb's macro-indicators capability.
_DEFAULT_CHAIN = [
    "finnhub", "yfinance", "openbb", "alpaca", "databento", "justetf",
    "twelvedata", "tiingo", "alphavantage", "fred", "eod", "ecb_sdw", "massive"
]


def _build_openbb_news_providers(massive_secret: tuple[str, dict[str, Any]] | None) -> list[str]:
    """Build the OpenBB news provider list gated on available API keys."""
    openbb_news_providers: list[str] = ["yfinance"]
    if massive_secret:
        openbb_news_providers.append("polygon")
    return openbb_news_providers


def _build_provider(
    name: str,
    db: Session,
    public: dict[str, Any],
    settings: Any,
    secrets: dict[str, tuple[str, dict[str, Any]] | None],
) -> MarketDataProvider | None:
    """Build a single provider instance by name, or None if unsupported."""
    if name == "openbb":
        openbb_enabled = bool(public.get("openbb_enabled", False))
        openbb_url = public.get("openbb_api_url") or settings.openbb_api_url
        news_providers = _build_openbb_news_providers(secrets.get("massive"))
        # Pass the configurable default ODP provider (DB → env → "yfinance").
        default_provider = public.get("openbb_default_provider") or resolve_openbb_default_provider(db)
        return OpenBBProvider(
            enabled=openbb_enabled,
            api_url=openbb_url,
            news_providers=news_providers,
            default_provider=default_provider,
        )
    if name == "massive":
        massive_secret = secrets.get("massive")
        return MassiveProvider(massive_secret[0] if massive_secret else None)
    if name == "alphavantage":
        alpha_secret = secrets.get("alphavantage")
        return AlphaVantageProvider(alpha_secret[0] if alpha_secret else None)
    if name == "finnhub":
        finnhub_secret = secrets.get("finnhub")
        return FinnhubProvider(finnhub_secret[0] if finnhub_secret else None)
    if name == "fred":
        fred_secret = secrets.get("fred")
        return FredProvider(fred_secret[0] if fred_secret else None)
    if name == "eod":
        eod_secret = secrets.get("eod")
        return EodProvider(eod_secret[0] if eod_secret else None)
    if name == "ecb_sdw":
        return EcbProvider()
    if name == "yfinance":
        return YFinanceProvider()
    if name == "justetf":
        return JustETFProvider()
    if name == "twelvedata":
        td_secret = secrets.get("twelvedata")
        return TwelveDataProvider(td_secret[0] if td_secret else None)
    if name == "databento":
        db_secret = secrets.get("databento")
        return DatabentoProvider(db_secret[0] if db_secret else None)
    if name == "alpaca":
        alp_secret = secrets.get("alpaca")
        if alp_secret:
            api_key, meta = alp_secret
            return AlpacaProvider(api_key, api_secret=meta.get("api_secret"))
        return AlpacaProvider(None, None)
    if name == "tiingo":
        ti_secret = secrets.get("tiingo")
        return TiingoProvider(ti_secret[0] if ti_secret else None)
    return None


def build_provider_registry(db: Session) -> ProviderRegistry:
    global _registry_cache

    # Check cache
    now = time.monotonic()
    if _registry_cache is not None:
        cached_at, cached_registry = _registry_cache
        if now - cached_at < _REGISTRY_CACHE_TTL:
            return cached_registry

    settings = get_settings()
    public = get_public_settings(db)

    # Load secrets once and pass around
    secrets: dict[str, tuple[str, dict[str, Any]] | None] = {
        "alphavantage": get_secret(db, "alphavantage"),
        "finnhub": get_secret(db, "finnhub"),
        "fred": get_secret(db, "fred"),
        "eod": get_secret(db, "eod"),
        "massive": get_secret(db, "massive"),
        "twelvedata": get_secret(db, "twelvedata"),
        "databento": get_secret(db, "databento"),
        "alpaca": get_secret(db, "alpaca"),
        "tiingo": get_secret(db, "tiingo"),
    }

    # Parse provider chain from settings, falling back to default order
    chain_raw = public.get("provider_chain_json")
    if chain_raw:
        try:
            chain = json.loads(chain_raw)
            if not isinstance(chain, list):
                chain = _DEFAULT_CHAIN
        except json.JSONDecodeError:
            chain = _DEFAULT_CHAIN
    else:
        chain = _DEFAULT_CHAIN

    providers: list[MarketDataProvider] = []
    for name in chain:
        prov = _build_provider(name, db, public, settings, secrets)
        if prov is not None:
            providers.append(prov)

    registry = ProviderRegistry(providers)
    _registry_cache = (time.monotonic(), registry)
    return registry


def invalidate_registry_cache() -> None:
    """Force the registry to be rebuilt on next access."""
    global _registry_cache
    _registry_cache = None
