"""Dispersion metrics (variation within composite portfolios)."""

import numpy as np
from dataclasses import dataclass


@dataclass
class DispersionMetrics:
    """Dispersion metrics across composite members."""
    daily_dispersion: float
    monthly_dispersion: float
    count_portfolios: int


def asset_weighted_dispersion(
    portfolio_returns: list[dict],
    portfolio_values: list[dict],
) -> DispersionMetrics:
    """
    Calculate asset-weighted dispersion (std dev of member returns).

    Dispersion = sqrt(sum(w_i * (r_i - r_composite)^2))
    where w_i = value weight of member i

    Args:
        portfolio_returns: [{portfolio_id, date, return}]
        portfolio_values: [{portfolio_id, date, value}] for weighting

    Returns:
        DispersionMetrics with daily and monthly dispersion
    """
    if not portfolio_returns or not portfolio_values:
        return DispersionMetrics(
            daily_dispersion=0.0,
            monthly_dispersion=0.0,
            count_portfolios=0,
        )

    # Group returns by date
    returns_by_date = {}
    for ret_data in portfolio_returns:
        date_key = ret_data["date"]
        if date_key not in returns_by_date:
            returns_by_date[date_key] = {}
        returns_by_date[date_key][ret_data["portfolio_id"]] = ret_data["return"]

    # Group values by date for weighting
    values_by_date = {}
    for val_data in portfolio_values:
        date_key = val_data["date"]
        if date_key not in values_by_date:
            values_by_date[date_key] = {}
        values_by_date[date_key][val_data["portfolio_id"]] = val_data["value"]

    # Calculate daily dispersion
    daily_dispersions = []

    for date_key, portfolio_rets in returns_by_date.items():
        if date_key not in values_by_date:
            continue

        portfolio_vals = values_by_date[date_key]
        portfolios = [pid for pid in portfolio_rets.keys() if pid in portfolio_vals]

        if not portfolios:
            continue

        # Calculate weights and composite return
        total_value = sum(portfolio_vals[pid] for pid in portfolios)
        if total_value == 0:
            continue

        weights = [portfolio_vals[pid] / total_value for pid in portfolios]
        returns = [portfolio_rets[pid] for pid in portfolios]
        composite_return = sum(w * r for w, r in zip(weights, returns))

        # Dispersion = sqrt(sum(w_i * (r_i - r_composite)^2))
        variance = sum(w * (r - composite_return) ** 2 for w, r in zip(weights, returns))
        dispersion = np.sqrt(variance) if variance > 0 else 0.0
        daily_dispersions.append(dispersion)

    # Calculate monthly dispersion (aggregate daily dispersions)
    # For simplicity, average daily dispersions
    daily_disp_avg = np.mean(daily_dispersions) if daily_dispersions else 0.0
    monthly_disp_avg = daily_disp_avg * np.sqrt(21)  # Rough approximation for trading days

    return DispersionMetrics(
        daily_dispersion=float(daily_disp_avg),
        monthly_dispersion=float(monthly_disp_avg),
        count_portfolios=len(set(p for ret_data in portfolio_returns for p in [ret_data["portfolio_id"]])),
    )
