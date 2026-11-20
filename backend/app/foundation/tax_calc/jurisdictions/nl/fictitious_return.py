"""NL Box 3 fictitious return rates and tax brackets.

These rates are set annually by the Belastingdienst per Dutch tax law
(Article 5c, Corporate Income Tax Act). Rates apply to wealth assets regardless of actual returns.

MAINTENANCE REQUIRED: Update FICTITIOUS_RETURN_TABLE and BUCKET_RATES each January when
Belastingdienst publishes the new year's rates in the Landsdecreet Inkomstenbelasting.

Reformed Box 3 (2023+): Three separate buckets have distinct fictitious return rates.
"""

from decimal import Decimal

# Per-bucket fictitious return rates (reformed Box 3, 2023+)
# Source: Belastingdienst published rates
# Format: {year: {"bank": rate_pct, "investment": rate_pct, "debt": rate_pct}}
BUCKET_RATES: dict[int, dict[str, Decimal]] = {
    2023: {
        "bank": Decimal("0.36"),       # 0.36% for bank/savings deposits
        "investment": Decimal("6.17"), # 6.17% for investments
        "debt": Decimal("2.57"),       # 2.57% for debts (deductible rate)
    },
    2024: {
        "bank": Decimal("1.03"),       # 1.03% for bank/savings deposits
        "investment": Decimal("6.04"), # 6.04% for investments
        "debt": Decimal("2.47"),       # 2.47% for debts (deductible rate)
    },
    2025: {
        "bank": Decimal("1.44"),       # 1.44% for bank/savings deposits (provisional)
        "investment": Decimal("5.88"), # 5.88% for investments (provisional)
        "debt": Decimal("2.62"),       # 2.62% for debts (provisional)
    },
    2026: {
        "bank": Decimal("1.44"),       # placeholder — update when Belastingdienst publishes
        "investment": Decimal("5.88"),
        "debt": Decimal("2.62"),
    },
}

# Fictitious return rates by year and wealth bracket (legacy single-rate table — kept for
# compatibility; the reformed multi-bucket model uses BUCKET_RATES instead)
FICTITIOUS_RETURN_TABLE: dict[int, list[tuple[Decimal, Decimal]]] = {
    2023: [
        (Decimal("0"), Decimal("0.00")),
        (Decimal("57000"), Decimal("2.92")),
    ],
    2024: [
        (Decimal("0"), Decimal("0.00")),
        (Decimal("57000"), Decimal("2.92")),
    ],
    2025: [
        (Decimal("0"), Decimal("0.00")),
        (Decimal("57000"), Decimal("2.92")),
    ],
    2026: [
        (Decimal("0"), Decimal("0.00")),       # €0 - threshold 1: 0%
        (Decimal("57000"), Decimal("2.92")),   # €57k - threshold 2: 2.92%
        (Decimal("220000"), Decimal("6.04")),  # €220k - threshold 3: 6.04%
        (Decimal("720000"), Decimal("5.53")),  # €720k+: 5.53%
    ],
}

# Tax brackets for Box 3
BOX3_TAX_BRACKETS: dict[int, list[tuple[Decimal, Decimal]]] = {
    2023: [
        (Decimal("0"), Decimal("0.00")),
        (Decimal("57000"), Decimal("32.00")),  # 32% in 2023
    ],
    2024: [
        (Decimal("0"), Decimal("0.00")),
        (Decimal("57000"), Decimal("36.00")),  # 36% in 2024
    ],
    2025: [
        (Decimal("0"), Decimal("0.00")),
        (Decimal("57000"), Decimal("36.00")),  # 36% in 2025
    ],
    2026: [
        (Decimal("0"), Decimal("0.00")),        # €0-€73.8k: 0% (covered by heffingvrij vermogen)
        (Decimal("73800"), Decimal("36.55")),   # €73.8k+: 36.55%
    ],
}

# Tax-free allowance (heffingvrij vermogen) - annual
HEFFINGVRIJ_VERMOGEN: dict[int, Decimal] = {
    2023: Decimal("57000"),
    2024: Decimal("57000"),
    2025: Decimal("57000"),
    2026: Decimal("57000"),
}


def get_bucket_rates(year: int) -> dict[str, Decimal]:
    """Get the per-bucket fictitious return rates for the reformed Box 3 model.

    Args:
        year: Tax year.

    Returns:
        Dict with keys 'bank', 'investment', 'debt' → rate as a decimal percentage.

    Raises:
        ValueError: If year's bucket rates are missing.
    """
    if year not in BUCKET_RATES:
        raise ValueError(
            f"Per-bucket fictitious return rates missing for year {year}. "
            f"TODO: Update BUCKET_RATES in {__file__} when Belastingdienst publishes new rates."
        )
    return BUCKET_RATES[year]


def get_fictitious_return_rate(
    wealth_eur: Decimal, year: int
) -> Decimal:
    """Get the fictitious return rate (%) for a given wealth bracket in a given year.

    Args:
        wealth_eur: Total wealth in EUR (before tax-free allowance).
        year: Tax year.

    Returns:
        Fictitious return rate as a decimal percentage (e.g., Decimal("2.92")).

    Raises:
        ValueError: If year's rate table is missing.
    """
    if year not in FICTITIOUS_RETURN_TABLE:
        raise ValueError(
            f"Fictitious return rate table missing for year {year}. "
            f"TODO: Update FICTITIOUS_RETURN_TABLE in {__file__} "
            f"when Belastingdienst publishes new rates."
        )

    brackets = FICTITIOUS_RETURN_TABLE[year]
    applicable_rate = brackets[-1][1]  # Default to highest bracket

    for threshold, rate in brackets:
        if wealth_eur < threshold:
            applicable_rate = brackets[brackets.index((threshold, rate)) - 1][1]
            break
        applicable_rate = rate

    return applicable_rate


def calculate_box3_taxable_wealth(
    total_wealth_eur: Decimal, year: int
) -> Decimal:
    """Calculate taxable wealth for Box 3 after heffingvrij vermogen allowance.

    Args:
        total_wealth_eur: Total wealth in EUR.
        year: Tax year.

    Returns:
        Taxable wealth (total - tax-free allowance).

    Raises:
        ValueError: If year's allowance is missing.
    """
    if year not in HEFFINGVRIJ_VERMOGEN:
        raise ValueError(f"Tax-free allowance (heffingvrij vermogen) missing for year {year}.")

    allowance = HEFFINGVRIJ_VERMOGEN[year]
    return max(Decimal("0"), total_wealth_eur - allowance)


def calculate_box3_tax(
    taxable_wealth_eur: Decimal, year: int
) -> Decimal:
    """Calculate Box 3 tax on taxable wealth.

    Args:
        taxable_wealth_eur: Wealth after heffingvrij vermogen deduction.
        year: Tax year.

    Returns:
        Box 3 tax amount in EUR.

    Raises:
        ValueError: If year's tax bracket table is missing.
    """
    if year not in BOX3_TAX_BRACKETS:
        raise ValueError(f"Box 3 tax bracket table missing for year {year}.")

    brackets = BOX3_TAX_BRACKETS[year]

    # Find applicable bracket (simplified: single-bracket system for NL Box 3)
    tax_rate = brackets[-1][1]  # Default to highest bracket
    for threshold, rate in brackets:
        if taxable_wealth_eur < threshold:
            # Use previous bracket's rate
            idx = brackets.index((threshold, rate))
            if idx > 0:
                tax_rate = brackets[idx - 1][1]
            break
        tax_rate = rate

    return (taxable_wealth_eur * tax_rate / Decimal("100")).quantize(Decimal("0.01"))
