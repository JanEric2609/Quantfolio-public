"""NL Box 3 (Wealth Tax) calculator for Dutch asset taxation (vermogensrendementsheffing).

Box 3 taxes the ownership of assets at a fictitious return rate, regardless of actual
income. This module implements:
- Asset classification (three buckets: bank deposits, investments, debts)
- Fictitious return calculation
- Tax-free allowance (heffingvrij vermogen) deduction
- Dual residency proration (for partial-year Dutch residency)
- FX conversion to EUR

Reference: Article 5c, Dutch Corporate Income Tax Act (Vennootschapsbelastingwet).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from .allowances import get_heffingvrij_vermogen, prorate_heffingvrij_vermogen
from .fictitious_return import (
    calculate_box3_tax,
    get_bucket_rates,
    get_fictitious_return_rate,
)


@dataclass
class Box3WealthBucket:
    """One of the three asset buckets in NL Box 3."""

    bucket_type: Literal["bank_deposits", "investments", "debts"]
    amount_eur: Decimal
    description: str = ""


@dataclass
class Box3Calculation:
    """Result of a NL Box 3 tax calculation."""

    year: int
    total_wealth_eur: Decimal  # Before allowance
    heffingvrij_vermogen_eur: Decimal  # Full or prorated
    taxable_wealth_eur: Decimal  # After allowance
    fictitious_return_rate_pct: Decimal  # Blended/reference rate
    fictitious_return_eur: Decimal  # Total notional return across buckets
    box3_tax_eur: Decimal  # Final tax
    effective_tax_rate_pct: Decimal  # Actual rate on total wealth

    # Breakdown by bucket
    bank_deposits_eur: Decimal = Decimal("0")
    investments_eur: Decimal = Decimal("0")
    debts_eur: Decimal = Decimal("0")

    # Per-bucket fictitious returns (for display)
    bank_fictitious_return_eur: Decimal = Decimal("0")
    investment_fictitious_return_eur: Decimal = Decimal("0")
    debt_deduction_eur: Decimal = Decimal("0")

    # Per-bucket rates used
    bank_rate_pct: Decimal = Decimal("0")
    investment_rate_pct: Decimal = Decimal("0")
    debt_rate_pct: Decimal = Decimal("0")

    # Dual residency info (if applicable)
    days_resident_nl: int | None = None  # Days resident in NL during year
    days_in_year: int = 365
    is_prorated: bool = False

    # FX info (if applicable)
    fx_rate_end_of_year: Decimal = Decimal("1.00")  # USD/EUR or other


def classify_wealth_by_bucket(
    holdings: list[dict],
) -> tuple[Decimal, Decimal, Decimal]:
    """Classify holdings into Box 3 buckets and return their EUR values.

    Args:
        holdings: List of holdings with 'type', 'amount_eur', 'currency' keys.
                 Types: 'cash', 'equity', 'bond', 'fund', 'commodity', 'crypto', 'mortgage_debt'

    Returns:
        Tuple of (bank_deposits_eur, investments_eur, debts_eur).
    """
    bank_deposits = Decimal("0")
    investments = Decimal("0")
    debts = Decimal("0")

    for holding in holdings:
        h_type = holding.get("type", "").lower()
        h_amount = Decimal(str(holding.get("amount_eur", 0)))

        if h_type in ("cash", "bank_account"):
            bank_deposits += h_amount
        elif h_type in ("mortgage_debt", "loan"):
            debts += h_amount  # Negative (liability)
        else:  # equity, bond, fund, commodity, crypto, etc.
            investments += h_amount

    return bank_deposits, investments, debts


def calculate_box3(
    total_wealth_eur: Decimal,
    year: int,
    days_resident_nl: int | None = None,
    days_in_year: int = 365,
    bank_deposits_eur: Decimal = Decimal("0"),
    investments_eur: Decimal = Decimal("0"),
    debts_eur: Decimal = Decimal("0"),
) -> Box3Calculation:
    """Calculate NL Box 3 wealth tax using the reformed three-bucket model (2023+).

    Each bucket (bank deposits, investments, debts) has its own fictitious return rate
    set by the Belastingdienst each year. Tax is applied to the net fictitious return
    at the flat box3 tax rate.

    Args:
        total_wealth_eur: Total net wealth in EUR (assets - liabilities). If buckets are
                         provided this should equal bank_deposits_eur + investments_eur
                         - debts_eur; it is used for the effective-rate calculation.
        year: Tax year.
        days_resident_nl: If provided, prorate allowance for partial-year residence.
                         (Used during moves to/from NL; default=None means full year)
        days_in_year: Days in the tax year (365 or 366 for leap year).
        bank_deposits_eur: Bank deposits and cash (Bucket 1).
        investments_eur: Investments — equity, funds, bonds, crypto, etc. (Bucket 2).
        debts_eur: Liabilities (mortgages, loans). Pass as a positive number; it will
                   be subtracted. (Bucket 3)

    Returns:
        Box3Calculation with full tax breakdown.

    Raises:
        ValueError: If year's tax parameters are not defined.
    """
    # --- Bug 2a fix: compute prorated heffingvrij and use IT for the taxable-wealth
    # subtraction instead of calling calculate_box3_taxable_wealth() which would
    # internally subtract the FULL allowance, discarding the proration.
    if days_resident_nl is not None:
        heffingvrij = prorate_heffingvrij_vermogen(year, days_resident_nl, days_in_year)
        is_prorated = True
    else:
        heffingvrij = get_heffingvrij_vermogen(year)
        is_prorated = False

    # Gross assets base = bank + investments (debts reduce the taxable base)
    gross_assets = bank_deposits_eur + investments_eur
    net_wealth = gross_assets - debts_eur

    # Taxable wealth = net wealth after the (prorated) heffingvrij allowance
    # Bug 2a: use the already-computed heffingvrij directly rather than relying on
    # calculate_box3_taxable_wealth() which always deducts the full-year allowance.
    taxable_wealth = max(Decimal("0"), net_wealth - heffingvrij)

    # --- Bug 2b fix: three-bucket fictitious-return calculation ---
    try:
        bucket_rates = get_bucket_rates(year)
        bank_rate = bucket_rates["bank"]
        inv_rate = bucket_rates["investment"]
        debt_rate = bucket_rates["debt"]
    except ValueError:
        # Fallback: use legacy single-rate table if bucket rates not defined
        bank_rate = get_fictitious_return_rate(total_wealth_eur, year)
        inv_rate = bank_rate
        debt_rate = bank_rate

    bank_return = (bank_deposits_eur * bank_rate / Decimal("100")).quantize(Decimal("0.01"))
    inv_return = (investments_eur * inv_rate / Decimal("100")).quantize(Decimal("0.01"))
    debt_deduction = (debts_eur * debt_rate / Decimal("100")).quantize(Decimal("0.01"))
    net_fictitious_return = bank_return + inv_return - debt_deduction

    # If no buckets provided fall back to legacy blended-rate model
    has_bucket_data = (bank_deposits_eur + investments_eur + debts_eur) > Decimal("0")
    if not has_bucket_data:
        blended_rate = get_fictitious_return_rate(total_wealth_eur, year)
        net_fictitious_return = (total_wealth_eur * blended_rate / Decimal("100")).quantize(
            Decimal("0.01")
        )
        bank_rate = inv_rate = debt_rate = blended_rate

    # Box 3 tax rate applied to taxable fictitious return.
    # The tax base for the rate lookup is the taxable wealth (after allowance), but
    # under the reformed model the actual tax is computed on the fictitious-return amount
    # proportional to the taxable share of total wealth.
    if net_wealth > Decimal("0"):
        taxable_fraction = taxable_wealth / net_wealth
    else:
        taxable_fraction = Decimal("0")

    taxable_fictitious_return = (net_fictitious_return * taxable_fraction).quantize(
        Decimal("0.01")
    )

    # Apply the box3 flat tax rate from the brackets table
    # The brackets are keyed on taxable_wealth so that the correct marginal rate is found
    tax_eur = calculate_box3_tax(taxable_wealth, year)
    # Override with the fictitious-return method when bucket data is present, as the
    # reformed rules tax the fictitious return directly (not the wealth balance):
    if has_bucket_data:
        from .fictitious_return import BOX3_TAX_BRACKETS
        brackets = BOX3_TAX_BRACKETS.get(year, [])
        box3_tax_rate = brackets[-1][1] if brackets else Decimal("36")
        tax_eur = (taxable_fictitious_return * box3_tax_rate / Decimal("100")).quantize(
            Decimal("0.01")
        )

    # Blended/reference rate (for display — ratio of total fictitious return to total wealth)
    if total_wealth_eur > Decimal("0"):
        blended_rate_display = (
            net_fictitious_return / total_wealth_eur * Decimal("100")
        ).quantize(Decimal("0.0001"))
    else:
        blended_rate_display = Decimal("0")

    # Effective tax rate on total wealth
    if total_wealth_eur > Decimal("0"):
        effective_rate = (tax_eur / total_wealth_eur * Decimal("100")).quantize(
            Decimal("0.01")
        )
    else:
        effective_rate = Decimal("0")

    return Box3Calculation(
        year=year,
        total_wealth_eur=total_wealth_eur,
        heffingvrij_vermogen_eur=heffingvrij,
        taxable_wealth_eur=taxable_wealth,
        fictitious_return_rate_pct=blended_rate_display,
        fictitious_return_eur=net_fictitious_return,
        box3_tax_eur=tax_eur,
        effective_tax_rate_pct=effective_rate,
        bank_deposits_eur=bank_deposits_eur,
        investments_eur=investments_eur,
        debts_eur=debts_eur,
        bank_fictitious_return_eur=bank_return,
        investment_fictitious_return_eur=inv_return,
        debt_deduction_eur=debt_deduction,
        bank_rate_pct=bank_rate,
        investment_rate_pct=inv_rate,
        debt_rate_pct=debt_rate,
        days_resident_nl=days_resident_nl,
        days_in_year=days_in_year,
        is_prorated=is_prorated,
    )


def prorate_box3_dual_residency(
    year: int,
    date_moved_to_nl: date,
    date_moved_from_nl: date | None = None,
    total_wealth_eur: Decimal = Decimal("0"),
) -> tuple[int, Decimal]:
    """Calculate prorated days and tax-free allowance for dual residency.

    Used when the user moves to or from NL during a tax year.

    Args:
        year: Tax year.
        date_moved_to_nl: Date the user became NL resident for Box 3 purposes.
        date_moved_from_nl: Date the user left NL (if applicable).
        total_wealth_eur: Total wealth (used for Box 3 calculation later).

    Returns:
        Tuple of (days_resident_nl, heffingvrij_vermogen_eur).

    Raises:
        ValueError: If dates are invalid or outside the year.
    """
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)

    # Validate dates
    if date_moved_to_nl < year_start or date_moved_to_nl > year_end:
        raise ValueError(
            f"date_moved_to_nl ({date_moved_to_nl}) must be within year {year}"
        )

    if date_moved_from_nl is not None:
        if date_moved_from_nl < date_moved_to_nl or date_moved_from_nl > year_end:
            raise ValueError(
                f"date_moved_from_nl ({date_moved_from_nl}) must be between "
                f"{date_moved_to_nl} and {year_end}"
            )
        end_date = date_moved_from_nl
    else:
        end_date = year_end

    # Calculate days resident
    days_resident_nl = (end_date - date_moved_to_nl).days + 1

    # Prorate allowance
    heffingvrij = prorate_heffingvrij_vermogen(year, days_resident_nl, 365)

    return days_resident_nl, heffingvrij


def convert_foreign_holding_to_eur(
    amount_foreign: Decimal, currency: str, fx_rate_eur_per_unit: Decimal
) -> Decimal:
    """Convert a foreign-currency holding to EUR at year-end exchange rate.

    Args:
        amount_foreign: Amount in foreign currency.
        currency: Currency code (e.g., 'USD', 'GBP').
        fx_rate_eur_per_unit: Exchange rate (1 unit of currency = X EUR).

    Returns:
        Amount in EUR.
    """
    return (amount_foreign * fx_rate_eur_per_unit).quantize(Decimal("0.01"))
