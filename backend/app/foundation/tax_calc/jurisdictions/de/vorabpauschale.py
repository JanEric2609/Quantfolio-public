"""Vorabpauschale estimate per §18 InvStG 2018.

This is a simplified estimator. The official Basiszins is published by the
Bundesfinanzministerium each year. For years we do not know, we fall back to
the most recent known value and tag the result as ``estimate``.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal


# Official Basiszins published by BMF each January (§18 InvStG 2018).
# Update this dict every January once the BMF Schreiben is available.
BASISZINS_BY_YEAR: dict[int, Decimal] = {
    2018: Decimal("0.0087"),
    2019: Decimal("0.0052"),
    2020: Decimal("0.0007"),
    2021: Decimal("-0.0045"),
    2022: Decimal("-0.0005"),
    2023: Decimal("0.0255"),
    2024: Decimal("0.0229"),
    2025: Decimal("0.0253"),  # BMF Schreiben 10.01.2025
    2026: Decimal("0.0320"),  # BMF Schreiben 13.01.2026 — 3.20% official
}

# Statutory factor in §18 (3) InvStG: Basisertrag = 70% of Basiszins.
BASISERTRAG_FACTOR = Decimal("0.7")


def basiszins_for_year(year: int) -> tuple[Decimal, bool]:
    """Return (rate, known) for the given tax year."""
    if year in BASISZINS_BY_YEAR:
        return BASISZINS_BY_YEAR[year], True
    # Fall back to most recent known.
    latest = max(BASISZINS_BY_YEAR.keys())
    return BASISZINS_BY_YEAR[latest], False


def vorabpauschale_estimate(
    *,
    fund_value_start_of_year_eur: Decimal,
    fund_value_end_of_year_eur: Decimal,
    distributions_during_year_eur: Decimal,
    tax_year: int,
    months_held: int = 12,
) -> dict:
    """Estimate the Vorabpauschale (taxable presumptive yield) for a single fund.

    Formula (§18 InvStG):
        Basisertrag = fund_value_start * Basiszins * 0.7 * (months_held / 12)       (Abs. 1 S. 2, Abs. 2)
        Vorabpauschale = max(0, Basisertrag - distributions)                       (Abs. 1 S. 1)
        capped at the Mehrbetrag (end - start) + distributions                     (Abs. 1 S. 3)
    so a distributing fund can owe one in a year its price fell, as long as
    price change plus distributions was positive.
    """
    rate, known = basiszins_for_year(tax_year)
    # §18 Abs. 1 Satz 3 InvStG 2018: a negative Basiszins is treated as 0% for
    # computing Basisertrag (no Vorabpauschale is levied in negative-rate
    # years, e.g. 2021/2022). The true signed rate is still returned below for
    # display/transparency -- only the Basisertrag computation is floored.
    effective_rate = max(Decimal("0"), rate)
    month_factor = Decimal(min(max(months_held, 0), 12)) / Decimal("12")
    basisertrag = (
        fund_value_start_of_year_eur * effective_rate * BASISERTRAG_FACTOR * month_factor
    ).quantize(Decimal("0.01"))
    gain = (fund_value_end_of_year_eur - fund_value_start_of_year_eur).quantize(Decimal("0.01"))
    distributions = max(Decimal("0"), distributions_during_year_eur)
    mehrbetrag = gain + distributions
    vorab = max(Decimal("0"), min(basisertrag - distributions, mehrbetrag)).quantize(Decimal("0.01"))
    return {
        "basiszins": float(rate),
        "basiszins_known": known,
        "basisertrag_eur": float(basisertrag),
        "fund_gain_eur": float(gain),
        "distributions_eur": float(distributions_during_year_eur),
        "vorabpauschale_eur": float(vorab),
        "months_held": int(months_held),
        "tax_year": tax_year,
        "confidence": "estimate",
    }


def first_working_day(year: int) -> date:
    """When the bank books the Vorabpauschale: the first working day of ``year`` (Sec. 18(3) InvStG).

    1 January is a holiday everywhere; weekends are skipped. Regional holidays
    (6 January in some states) are not, which can only move the date one day early.
    """
    day = date(year, 1, 2)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def vorabpauschale_credit(
    *,
    acquired_at: date,
    sold_at: date,
    quantity: Decimal,
    per_unit_by_year: Mapping[int, Decimal],
) -> Decimal:
    """Vorabpauschalen already taxed on ``quantity`` units, deducted from the sale gain.

    Sec. 19(1) sentence 3 InvStG: the gain on a sale is reduced by the
    Vorabpauschalen set during the holding period, so they are not taxed twice.
    The one for fund year ``y`` counts once it was received, on the first
    working day of ``y + 1``; in the year of purchase it is reduced by one
    twelfth for every full month before the purchase (Sec. 18(2) InvStG).
    ``per_unit_by_year`` holds the full-year Vorabpauschale per unit, before
    Teilfreistellung.
    """
    total = Decimal("0")
    for year, per_unit in per_unit_by_year.items():
        if year < acquired_at.year or first_working_day(year + 1) > sold_at or per_unit <= 0:
            continue
        months = 12 - acquired_at.month + 1 if year == acquired_at.year else 12
        total += per_unit * quantity * Decimal(months) / Decimal(12)
    return total.quantize(Decimal("0.01"))
