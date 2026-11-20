"""Income tax tariff (Sec. 32a EStG) and the Guenstigerpruefung (Sec. 32d(6) EStG).

Capital income is taxed at the flat 25 % (Abgeltungsteuer) unless the
taxpayer asks, in the return, for the personal tariff to be applied instead:
the Finanzamt then charges whichever is lower. For a student with no other
income, capital income up to the Grundfreibetrag plus the Sparer-Pauschbetrag
plus the Sonderausgaben-Pauschbetrag costs nothing under the tariff, so any
KESt the banks withheld comes back with the return.

Tariff coefficients are verbatim from the statute, read 2026-10-04:
2026 from gesetze-im-internet.de (Sec. 32a(1) as amended by the
Steuerfortentwicklungsgesetz, BGBl. 2024 I Nr. 449, Art. 2), 2025 from the
same act's Art. 1, 2024 from the Gesetz zur steuerlichen Freistellung des
Existenzminimums 2024. Update each year with the Basiszins (AGENTS.md).

All amounts EUR. Estimates only; the tax assessment is the source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from .allowances import KEST_RATE, SPARER_PAUSCHBETRAG_SINGLE, SPARER_PAUSCHBETRAG_SPOUSE

ZERO = Decimal("0")
# Sec. 10c EStG: deducted from every taxpayer's total income, doubled for spouses.
SONDERAUSGABEN_PAUSCHBETRAG = Decimal("36")


@dataclass(frozen=True)
class Tariff:
    grundfreibetrag: Decimal
    zone2_end: Decimal  # y-zone: (a2 * y + 1400) * y
    a2: Decimal
    zone3_end: Decimal  # z-zone: (a3 * z + 2397) * z + c3
    a3: Decimal
    c3: Decimal
    c4: Decimal  # 0.42 x - c4
    zone5_start: Decimal
    c5: Decimal  # 0.45 x - c5


TARIFF_BY_YEAR: dict[int, Tariff] = {
    2024: Tariff(Decimal("11784"), Decimal("17005"), Decimal("954.80"), Decimal("66760"), Decimal("181.19"),
                 Decimal("991.21"), Decimal("10636.31"), Decimal("277826"), Decimal("18971.06")),
    2025: Tariff(Decimal("12096"), Decimal("17443"), Decimal("932.30"), Decimal("68480"), Decimal("176.64"),
                 Decimal("1015.13"), Decimal("10911.92"), Decimal("277826"), Decimal("19246.67")),
    2026: Tariff(Decimal("12348"), Decimal("17799"), Decimal("914.51"), Decimal("69878"), Decimal("173.10"),
                 Decimal("1034.87"), Decimal("11135.63"), Decimal("277826"), Decimal("19470.38")),
}


def tariff_for(year: int) -> tuple[Tariff, bool]:
    """The year's tariff, or the nearest known one (flagged as assumed)."""
    if year in TARIFF_BY_YEAR:
        return TARIFF_BY_YEAR[year], False
    known = max((y for y in TARIFF_BY_YEAR if y <= year), default=min(TARIFF_BY_YEAR))
    return TARIFF_BY_YEAR[known], True


def income_tax(zve: Decimal, year: int, *, spouse: bool = False) -> Decimal:
    """Tarifliche Einkommensteuer on ``zve`` (Sec. 32a(1), splitting under (5))."""
    if spouse:
        return income_tax(zve / 2, year) * 2
    t, _ = tariff_for(year)
    x = Decimal(max(ZERO, zve)).to_integral_value(rounding=ROUND_FLOOR)
    if x <= t.grundfreibetrag:
        tax = ZERO
    elif x <= t.zone2_end:
        y = (x - t.grundfreibetrag) / Decimal("10000")
        tax = (t.a2 * y + Decimal("1400")) * y
    elif x <= t.zone3_end:
        z = (x - t.zone2_end) / Decimal("10000")
        tax = (t.a3 * z + Decimal("2397")) * z + t.c3
    elif x < t.zone5_start:
        tax = Decimal("0.42") * x - t.c4
    else:
        tax = Decimal("0.45") * x - t.c5
    return max(ZERO, tax).to_integral_value(rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class Guenstiger:
    """Flat tax vs. tariff on the capital income of one year."""

    year: int
    tariff_assumed: bool
    capital_taxable_eur: Decimal  # after Teilfreistellung and the Sparer-Pauschbetrag
    flat_tax_eur: Decimal  # KESt only (Soli and church tax follow it)
    tariff_tax_eur: Decimal  # extra income tax the capital income causes at the personal rate
    use_tariff: bool  # the return should ask for the Guenstigerpruefung

    @property
    def final_tax_eur(self) -> Decimal:
        return min(self.flat_tax_eur, self.tariff_tax_eur)


def guenstigerpruefung(
    year: int,
    *,
    capital_income_eur: Decimal,
    other_income_eur: Decimal = ZERO,
    spouse: bool = False,
    other_deductions_eur: Decimal = ZERO,
) -> Guenstiger:
    """Compare the flat 25 % with the tariff on ``capital_income_eur``.

    ``capital_income_eur`` is after Teilfreistellung and loss offsetting but
    before the Sparer-Pauschbetrag. ``other_income_eur`` is the total of the
    other Einkuenfte (after Werbungskosten); ``other_deductions_eur`` covers
    Vorsorgeaufwendungen and the like, beyond the Sonderausgaben-Pauschbetrag
    that is always deducted. Church tax is ignored on both sides.
    """
    _, assumed = tariff_for(year)
    pausch = SPARER_PAUSCHBETRAG_SPOUSE if spouse else SPARER_PAUSCHBETRAG_SINGLE
    sonder = SONDERAUSGABEN_PAUSCHBETRAG * (2 if spouse else 1)
    capital = max(ZERO, capital_income_eur - pausch)
    deductions = sonder + max(ZERO, other_deductions_eur)
    base_without = max(ZERO, other_income_eur - deductions)
    base_with = max(ZERO, other_income_eur + capital - deductions)
    tariff_tax = income_tax(base_with, year, spouse=spouse) - income_tax(base_without, year, spouse=spouse)
    flat = (capital * KEST_RATE).quantize(Decimal("0.01"))
    return Guenstiger(
        year=year, tariff_assumed=assumed, capital_taxable_eur=capital, flat_tax_eur=flat,
        tariff_tax_eur=tariff_tax, use_tariff=tariff_tax < flat,
    )
