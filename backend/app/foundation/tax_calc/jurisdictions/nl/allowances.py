"""NL Box 3 tax-free allowance (heffingvrij vermogen) and deductions."""

from decimal import Decimal

from .fictitious_return import HEFFINGVRIJ_VERMOGEN


def get_heffingvrij_vermogen(year: int) -> Decimal:
    """Get the annual tax-free wealth allowance (heffingvrij vermogen) for a given year.

    Args:
        year: Tax year.

    Returns:
        Tax-free allowance in EUR.

    Raises:
        ValueError: If year is not in the table.
    """
    if year not in HEFFINGVRIJ_VERMOGEN:
        raise ValueError(
            f"Tax-free allowance (heffingvrij vermogen) not defined for year {year}. "
            f"This must be updated annually by the Belastingdienst."
        )
    return HEFFINGVRIJ_VERMOGEN[year]


def prorate_heffingvrij_vermogen(
    year: int, days_resident: int, days_in_year: int = 365
) -> Decimal:
    """Prorate the tax-free allowance for partial-year residence.

    Used during moves to/from NL Box 3 jurisdiction. The allowance is reduced
    proportionally for the days lived in NL.

    Args:
        year: Tax year.
        days_resident: Number of days resident in NL (for Box 3 purposes).
        days_in_year: Days in the year (normally 365, or 366 for leap years).

    Returns:
        Prorated tax-free allowance in EUR.

    Raises:
        ValueError: If year or day counts are invalid.
    """
    if days_resident < 0 or days_resident > days_in_year:
        raise ValueError(
            f"Invalid days_resident: {days_resident} "
            f"(must be between 0 and {days_in_year})"
        )

    full_allowance = get_heffingvrij_vermogen(year)
    prorated = (full_allowance * Decimal(days_resident) / Decimal(days_in_year)).quantize(
        Decimal("0.01")
    )
    return prorated
