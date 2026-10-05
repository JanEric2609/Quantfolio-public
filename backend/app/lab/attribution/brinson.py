"""Brinson-Fachler attribution decomposition.

Decomposes active return into allocation and selection effects.
"""

from dataclasses import dataclass


@dataclass
class SecurityAttribution:
    """Attribution metrics for a single security."""
    isin: str
    ticker: str
    name: str
    portfolio_weight: float
    benchmark_weight: float
    portfolio_return: float
    benchmark_return: float
    allocation_effect: float
    selection_effect: float
    interaction_effect: float

    @property
    def total_effect(self) -> float:
        """Total contribution to active return."""
        return self.allocation_effect + self.selection_effect + self.interaction_effect


@dataclass
class BrinsonResult:
    """Complete Brinson-Fachler decomposition."""
    allocation_effect: float
    selection_effect: float
    interaction_effect: float
    total_active_return: float
    securities: list[SecurityAttribution]


def brinson_fachler(
    portfolio_holdings: list[dict],
    benchmark_holdings: list[dict],
    portfolio_returns: dict[str, float],
    benchmark_returns: dict[str, float],
) -> BrinsonResult:
    """
    Decompose active return using Brinson-Fachler attribution.

    Args:
        portfolio_holdings: [{isin, ticker, name, value}] (total portfolio value sum)
        benchmark_holdings: [{isin, ticker, name, value}] (total benchmark value sum)
        portfolio_returns: {isin: return_pct} (security-level returns)
        benchmark_returns: {isin: return_pct} (security-level returns)

    Returns:
        BrinsonResult with allocation/selection/interaction effects

    Math:
        Allocation Effect = (w_p - w_b) * (r_b - r_bench_total)
        Selection Effect = w_b * (r_p - r_b)
        Interaction Effect = (w_p - w_b) * (r_p - r_b)
        Active Return = Allocation + Selection + Interaction
    """
    # Pre-aggregate holdings by ISIN (sum values across lots) so that duplicate
    # entries within a list (e.g. two lots of the same security) are combined
    # before weights are computed.  Also capture the first-seen ticker/name for
    # use in the output rows.
    portfolio_agg: dict[str, float] = {}
    portfolio_meta: dict[str, dict] = {}
    for h in portfolio_holdings:
        isin = h["isin"]
        portfolio_agg[isin] = portfolio_agg.get(isin, 0.0) + h["value"]
        if isin not in portfolio_meta:
            portfolio_meta[isin] = {"ticker": h.get("ticker", ""), "name": h.get("name", "")}

    benchmark_agg: dict[str, float] = {}
    benchmark_meta: dict[str, dict] = {}
    for h in benchmark_holdings:
        isin = h["isin"]
        benchmark_agg[isin] = benchmark_agg.get(isin, 0.0) + h["value"]
        if isin not in benchmark_meta:
            benchmark_meta[isin] = {"ticker": h.get("ticker", ""), "name": h.get("name", "")}

    # Build weight dictionaries from the aggregated values
    portfolio_value = sum(portfolio_agg.values())
    portfolio_weights = {
        isin: (v / portfolio_value if portfolio_value > 0 else 0.0)
        for isin, v in portfolio_agg.items()
    }

    benchmark_value = sum(benchmark_agg.values())
    benchmark_weights = {
        isin: (v / benchmark_value if benchmark_value > 0 else 0.0)
        for isin, v in benchmark_agg.items()
    }

    # Calculate total benchmark return
    benchmark_total_return = sum(
        benchmark_weights.get(isin, 0.0) * benchmark_returns.get(isin, 0.0)
        for isin in benchmark_weights.keys()
    )

    # Attribution by security — iterate over the union of unique ISINs once
    securities = []
    allocation_effect_sum = 0.0
    selection_effect_sum = 0.0
    interaction_effect_sum = 0.0

    all_isins = set(portfolio_weights.keys()) | set(benchmark_weights.keys())

    for isin in all_isins:
        w_p = portfolio_weights.get(isin, 0.0)
        w_b = benchmark_weights.get(isin, 0.0)
        r_p = portfolio_returns.get(isin, 0.0)
        r_b = benchmark_returns.get(isin, 0.0)

        # Prefer portfolio metadata; fall back to benchmark metadata
        meta = portfolio_meta.get(isin) or benchmark_meta.get(isin) or {}

        # Brinson decomposition
        allocation_eff = (w_p - w_b) * (r_b - benchmark_total_return)
        selection_eff = w_b * (r_p - r_b)
        interaction_eff = (w_p - w_b) * (r_p - r_b)

        allocation_effect_sum += allocation_eff
        selection_effect_sum += selection_eff
        interaction_effect_sum += interaction_eff

        securities.append(
            SecurityAttribution(
                isin=isin,
                ticker=meta.get("ticker", ""),
                name=meta.get("name", ""),
                portfolio_weight=w_p,
                benchmark_weight=w_b,
                portfolio_return=r_p,
                benchmark_return=r_b,
                allocation_effect=allocation_eff,
                selection_effect=selection_eff,
                interaction_effect=interaction_eff,
            )
        )

    total_active_return = allocation_effect_sum + selection_effect_sum + interaction_effect_sum

    # Sort by total effect descending
    securities.sort(key=lambda s: abs(s.total_effect), reverse=True)

    return BrinsonResult(
        allocation_effect=allocation_effect_sum,
        selection_effect=selection_effect_sum,
        interaction_effect=interaction_effect_sum,
        total_active_return=total_active_return,
        securities=securities,
    )
