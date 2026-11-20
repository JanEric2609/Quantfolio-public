"""Factor-based attribution using Fama-French factors."""

from dataclasses import dataclass
from datetime import date, datetime
from app.foundation import quant_factors


@dataclass
class FactorContribution:
    """Contribution of a factor to active return."""
    factor_name: str
    exposure: float
    contribution: float
    excess_return: float


@dataclass
class FactorAttributionResult:
    """Factor-based attribution results."""
    factors: list[FactorContribution]
    residual: float
    r_squared: float
    total_attribution: float


def factor_attribution(
    portfolio_returns: list[float],
    benchmark_returns: list[float],
    date_from: str,
    date_to: str,
    factors: list[str] | None = None,
    dates: list[str] | None = None,
) -> FactorAttributionResult:
    """
    Decompose active return by factor exposure.

    Uses Fama-French factor data and OLS regression to attribute
    active return to factor exposures.

    Args:
        portfolio_returns: Daily returns array
        benchmark_returns: Daily returns array
        date_from: Start date (YYYY-MM-DD)
        date_to: End date (YYYY-MM-DD)
        factors: List of factor names (default: ["mkt_rf", "smb", "hml"])
        dates: Optional ``YYYY-MM-DD`` date of each ``portfolio_returns`` entry.
            When given, the regression joins returns and factors on date. The
            Fama-French file lags the price history by a month or more, so
            the positional pairing used without dates misaligns the tail.

    Returns:
        FactorAttributionResult with factor contributions
    """
    if factors is None:
        factors = ["mkt_rf", "smb", "hml"]

    # Parse the requested window and pass it through so we only pull the
    # Fama-French history actually needed, instead of the entire series.
    start_date: date | None = (
        datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
    )
    end_date: date | None = (
        datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None
    )

    # Get Fama-French factor data
    ff_data = quant_factors.get_ff3_returns(start=start_date, end=end_date)
    if not ff_data:
        return FactorAttributionResult(
            factors=[],
            residual=0.0,
            r_squared=0.0,
            total_attribution=0.0,
        )

    # Compute active return
    active_returns = [p - b for p, b in zip(portfolio_returns, benchmark_returns)]

    # Use quant_factors' OLS-based attribution to get exposures
    # Convert ff_data factor dicts (date → return) to list format aligned by date
    ff_factors: dict[str, list[float]] = {}
    ff_by_date: dict[str, dict[str, float]] = {}
    if isinstance(ff_data, dict) and ff_data.get("status") == "completed":
        for f_name, f_vals in ff_data.get("factors", {}).items():
            ff_by_date[f_name] = f_vals
            sorted_dates = sorted(f_vals.keys())
            ff_factors[f_name] = [f_vals[d] for d in sorted_dates]
    if dates is not None and all(f in ff_by_date for f in factors):
        factor_returns_dict = quant_factors.compute_factor_attribution_by_date(
            portfolio_returns, dates, ff_by_date, factors
        )
    else:
        factor_returns_dict = quant_factors.compute_factor_attribution(
            portfolio_returns, ff_factors, factors
        )

    if not factor_returns_dict or factor_returns_dict.get("status") != "completed":
        return FactorAttributionResult(
            factors=[],
            residual=0.0,
            r_squared=0.0,
            total_attribution=0.0,
        )

    # Extract exposures and compute contributions
    exposures = factor_returns_dict.get("exposures", {})
    r_squared = factor_returns_dict.get("r_squared", 0.0)

    factor_contributions = []
    total_attribution = 0.0

    for factor_name in factors:
        exposure = exposures.get(factor_name, 0.0)
        factor_values = ff_data.get("factors", {}).get(factor_name, {})
        excess_return = sum(factor_values.values()) / len(factor_values) if factor_values else 0.0
        contribution = exposure * excess_return
        total_attribution += contribution

        factor_contributions.append(
            FactorContribution(
                factor_name=factor_name,
                exposure=exposure,
                contribution=contribution,
                excess_return=excess_return,
            )
        )

    # Residual is unexplained active return
    mean_active_return = sum(active_returns) / len(active_returns) if active_returns else 0.0
    residual = mean_active_return - total_attribution

    return FactorAttributionResult(
        factors=factor_contributions,
        residual=residual,
        r_squared=r_squared,
        total_attribution=total_attribution,
    )
