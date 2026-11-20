"""Money-Weighted Return (MWR) calculation.

MWR accounts for timing and size of external cashflows,
reflecting actual investor experience.
"""

import logging
from datetime import date
from typing import cast

logger = logging.getLogger(__name__)


def money_weighted_return(
    opening_value: float,
    closing_value: float,
    cashflows: list[dict] | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> float:
    """
    Calculate Money-Weighted Return using Modified Dietz formula.

    MWR = (Ending Value - Beginning Value - Net Cashflows) /
          (Beginning Value + sum(CF_i * weight_i))

    where weight_i = (days between CF and end) / total days

    Args:
        opening_value: Portfolio value at period start
        closing_value: Portfolio value at period end
        cashflows: [{date, amount}] positive = deposit, negative = withdrawal
        period_start: Period start date
        period_end: Period end date

    Returns:
        MWR as decimal (e.g., 0.10 for 10%)
    """
    if not period_start or not period_end or closing_value == 0:
        return 0.0

    if cashflows is None:
        cashflows = []

    # Total days in period
    total_days = (period_end - period_start).days
    if total_days == 0:
        return 0.0

    # Calculate weighted cashflows
    net_cf = 0.0
    weighted_cf = 0.0

    for cf in cashflows:
        if period_start <= cf["date"] <= period_end:
            net_cf += cf["amount"]
            # Weight = days from CF to end / total days
            days_to_end = (period_end - cf["date"]).days
            weight = days_to_end / total_days if total_days > 0 else 0.0
            weighted_cf += cf["amount"] * weight

    # Modified Dietz formula
    numerator = closing_value - opening_value - net_cf
    denominator = opening_value + weighted_cf

    if denominator == 0:
        return 0.0

    return numerator / denominator


def money_weighted_return_irr(
    opening_value: float,
    closing_value: float,
    cashflows: list[dict] | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> float:
    """
    Calculate Money-Weighted Return.

    Primary method: Modified Dietz.
    Fallback: Newton-Raphson / brentq IRR when Modified Dietz denominator ≤ 0
    or the result is unreasonably large (|MWR| > 10, i.e. >1000%).

    Solves NPV equation:
        0 = -start_value
            + sum(CF_i / (1 + r)^(t_i / 365))
            + end_value / (1 + r)^(T / 365)

    Args:
        opening_value: Portfolio value at period start
        closing_value: Portfolio value at period end
        cashflows: [{date, amount}] positive = deposit, negative = withdrawal
        period_start: Period start
        period_end: Period end

    Returns:
        MWR as decimal (e.g., 0.10 for 10%)
    """
    if not period_start or not period_end:
        return 0.0

    if cashflows is None:
        cashflows = []

    total_days = (period_end - period_start).days
    if total_days == 0:
        return 0.0

    # --- Primary: Modified Dietz ---
    mwr_dietz = money_weighted_return(
        opening_value, closing_value, cashflows, period_start, period_end
    )

    # Check whether Modified Dietz gave a reasonable result
    weighted_cf = sum(
        cf["amount"] * ((period_end - cf["date"]).days / total_days)
        for cf in cashflows
        if period_start <= cf["date"] <= period_end
    )
    denominator = opening_value + weighted_cf
    dietz_ok = denominator > 0 and abs(mwr_dietz) <= 10.0

    if dietz_ok:
        return mwr_dietz

    # --- Fallback: brentq IRR ---
    # Build cashflow timeline: (days_from_start, amount)
    cf_timeline: list[tuple[float, float]] = [
        ((cf["date"] - period_start).days, cf["amount"])
        for cf in cashflows
        if period_start <= cf["date"] <= period_end
    ]

    def npv(r: float) -> float:
        val = -opening_value
        for t_i, cf_i in cf_timeline:
            val += cf_i / (1.0 + r) ** (t_i / 365.0)
        val += closing_value / (1.0 + r) ** (total_days / 365.0)
        return val

    try:
        from scipy.optimize import brentq

        irr = cast(float, brentq(npv, -0.9999, 100.0, xtol=1e-8, maxiter=500))
        return float(irr)
    except Exception as exc:
        logger.debug("IRR brentq fallback failed (%s); returning Modified Dietz result", exc)
        return mwr_dietz
