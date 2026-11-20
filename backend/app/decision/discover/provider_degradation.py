"""Provider degradation assessor for the discovery pipeline.

Discovery candidates are scored not only on raw financial metrics but also on
how brittle their data sourcing was. A candidate whose price had to be fetched
from the third or fourth fallback provider is less trustworthy than one where
the primary provider responded immediately.

The degradation score is a simple 0.0-1.0 penalty:

* 0.0 = the first provider in the configured chain returned data.
* >0.0 = one or more earlier providers failed/skipped before a later one succeeded.
* 1.0 = every provider in the chain was exhausted and no data was returned.

This module is headless: it only depends on a SQLAlchemy Session so that
``ProviderRegistry`` can resolve settings/secrets and check per-provider health.
It performs no database writes and contains no FastAPI/HTTP code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.foundation.providers.registry import ProviderRegistry, build_provider_registry

logger = logging.getLogger(__name__)


@dataclass
class DegradationResult:
    """Degradation assessment for a single discovery candidate.

    Attributes:
        symbol: The candidate ticker/symbol that was assessed.
        degradation_score: 0.0 when the primary provider worked, 1.0 when all
            providers in the chain were exhausted, scaled linearly in between.
        provider_chain: Ordered list of provider names in the configured chain.
        fallbacks_used: Number of providers that had to be skipped/failed before
            the successful provider (0 if the first provider succeeded).
        warnings: Human-readable degradation warnings (e.g. fallback used).
    """

    symbol: str
    degradation_score: float
    provider_chain: list[str] = field(default_factory=list)
    fallbacks_used: int = 0
    warnings: list[str] = field(default_factory=list)


class DegradationAssessor:
    """Assess how far down the provider fallback chain a symbol had to go."""

    def __init__(self) -> None:
        pass

    def assess(self, symbol: str, db: Session) -> DegradationResult:
        """Assess provider degradation for a single symbol.

        Builds a fresh ``ProviderRegistry`` from *db* settings and delegates to
        ``_assess_with_registry``.

        Args:
            symbol: Candidate ticker/symbol to assess.
            db: SQLAlchemy session used to build the provider registry.

        Returns:
            ``DegradationResult`` with score, chain, fallback count and warnings.
        """
        registry = build_provider_registry(db)
        return self._assess_with_registry(symbol, registry)

    def _assess_with_registry(
        self, symbol: str, registry: ProviderRegistry
    ) -> DegradationResult:
        """Assess provider degradation for *symbol* using an existing registry."""
        provider_chain = [provider.name for provider in registry.providers]

        if not provider_chain:
            warning = f"No providers configured; cannot assess degradation for {symbol}."
            logger.warning(warning)
            return DegradationResult(
                symbol=symbol,
                degradation_score=1.0,
                provider_chain=[],
                fallbacks_used=0,
                warnings=[warning],
            )

        result = registry.get_quote(symbol)
        successful_provider = result.get("provider") if result.get("ok") else None

        if successful_provider is None or successful_provider == "registry":
            warning = (
                f"All providers exhausted for {symbol} "
                f"(chain: {' -> '.join(provider_chain)})."
            )
            logger.warning(warning)
            return DegradationResult(
                symbol=symbol,
                degradation_score=1.0,
                provider_chain=provider_chain,
                fallbacks_used=len(provider_chain),
                warnings=[warning],
            )

        try:
            fallback_index = provider_chain.index(successful_provider)
        except ValueError:
            # Successful provider is not in the configured chain; treat as worst
            # case but keep the actual provider name in the warning.
            warning = (
                f"Unexpected successful provider '{successful_provider}' for {symbol} "
                f"not found in configured chain ({' -> '.join(provider_chain)})."
            )
            logger.warning(warning)
            return DegradationResult(
                symbol=symbol,
                degradation_score=1.0,
                provider_chain=provider_chain,
                fallbacks_used=len(provider_chain),
                warnings=[warning],
            )

        max_index = max(1, len(provider_chain) - 1)
        degradation_score = fallback_index / max_index
        fallbacks_used = fallback_index

        warnings: list[str] = []
        if fallbacks_used > 0:
            warning = (
                f"Primary provider '{provider_chain[0]}' failed for {symbol}; "
                f"used fallback '{successful_provider}' "
                f"({fallbacks_used} fallback{'s' if fallbacks_used > 1 else ''} used)."
            )
            logger.warning(warning)
            warnings.append(warning)

        return DegradationResult(
            symbol=symbol,
            degradation_score=float(degradation_score),
            provider_chain=provider_chain,
            fallbacks_used=fallbacks_used,
            warnings=warnings,
        )

    def assess_batch(self, symbols: list[str], db: Session) -> list[DegradationResult]:
        """Assess provider degradation for multiple symbols sequentially.

        The registry is built once per batch and reused for every symbol so that
        settings/secrets are fresh without redundant construction.

        Args:
            symbols: Candidate tickers/symbols to assess.
            db: SQLAlchemy session used to build the provider registry.

        Returns:
            List of ``DegradationResult`` objects in the same order as *symbols*.
        """
        registry = build_provider_registry(db)
        results: list[DegradationResult] = []
        for symbol in symbols:
            try:
                results.append(self._assess_with_registry(symbol, registry))
            except Exception:
                logger.exception("Degradation assessment failed for %s", symbol)
                results.append(
                    DegradationResult(
                        symbol=symbol,
                        degradation_score=1.0,
                        provider_chain=[],
                        fallbacks_used=0,
                        warnings=[f"Degradation assessment crashed for {symbol}."],
                    )
                )
        return results
