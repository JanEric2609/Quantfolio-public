"""Sparer-Pauschbetrag, Soli, church tax — pure functions, EUR Decimals."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any


SPARER_PAUSCHBETRAG_SINGLE = Decimal("1000")
SPARER_PAUSCHBETRAG_SPOUSE = Decimal("2000")
KEST_RATE = Decimal("0.25")
SOLI_RATE = Decimal("0.055")  # of KESt


def sparer_pauschbetrag_from_settings(settings: Mapping[str, Any]) -> Decimal:
    """The yearly allowance the settings describe, doubled for spouses filing jointly.

    ``freistellungsauftrag_amount`` defaults to the single 1,000 EUR and the
    defaults are merged into every settings read, so a plain lookup never
    fell through to 2,000 EUR when ``tax_spouse_allowance`` was on (audit
    2026-11-15, 2.11). The single statutory amount with the spouse flag set
    reads as the joint amount; any other value is the owner's override.
    """
    spouse = bool(settings.get("tax_spouse_allowance"))
    raw = settings.get("freistellungsauftrag_amount")
    try:
        amount = Decimal(str(raw)) if raw not in (None, "") else None
    except (ArithmeticError, ValueError):
        amount = None
    if amount is None or amount <= 0:
        return SPARER_PAUSCHBETRAG_SPOUSE if spouse else SPARER_PAUSCHBETRAG_SINGLE
    if spouse and amount == SPARER_PAUSCHBETRAG_SINGLE:
        return SPARER_PAUSCHBETRAG_SPOUSE
    return amount


def apply_sparer_pauschbetrag(
    taxable_income_eur: Decimal,
    allowance_total_eur: Decimal,
    already_used_eur: Decimal = Decimal("0"),
) -> tuple[Decimal, Decimal]:
    """Reduce taxable income by the remaining Freistellungsauftrag.

    Returns ``(taxable_after, used_now)``. Allowance does not go negative.
    """
    if taxable_income_eur <= 0:
        return taxable_income_eur, Decimal("0")
    remaining = max(Decimal("0"), allowance_total_eur - already_used_eur)
    used = min(remaining, taxable_income_eur)
    return taxable_income_eur - used, used


def soli_amount(kest_amount_eur: Decimal) -> Decimal:
    return (kest_amount_eur * SOLI_RATE).quantize(Decimal("0.01"))


def kirchensteuer_amount(kest_amount_eur: Decimal, church_rate: Decimal) -> Decimal:
    """Church tax is charged on the KESt amount, not on income."""
    if church_rate <= 0:
        return Decimal("0")
    return (kest_amount_eur * church_rate).quantize(Decimal("0.01"))


def reduced_kest_amount(
    base_eur: Decimal,
    foreign_wht_creditable_eur: Decimal,
    church_rate: Decimal,
) -> Decimal:
    """Effective KESt under §32d(1) EStG when church tax is owed.

    Church tax is deductible as a Sonderausgabe, so the effective
    capital-gains tax is lowered. The statutory closed form folds the
    foreign-WHT credit and the church-rate deduction together::

        KESt = (base - 4 * foreign_wht_creditable) / (4 + church_rate)

    With ``church_rate == 0`` this reduces exactly to ``base * 0.25 -
    foreign_wht_creditable``, i.e. flat 25% KESt minus the foreign-WHT credit,
    so the non-church path is unchanged. Floored at zero.
    """
    numerator = base_eur - Decimal("4") * foreign_wht_creditable_eur
    kest = numerator / (Decimal("4") + church_rate)
    return max(Decimal("0"), kest.quantize(Decimal("0.01")))
