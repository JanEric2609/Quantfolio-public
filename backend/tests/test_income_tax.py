"""Sec. 32a tariff and the Guenstigerpruefung (Sec. 32d(6) EStG)."""
from decimal import Decimal as D

import pytest

from app.foundation.tax_calc.jurisdictions.de.income_tax import (
    SONDERAUSGABEN_PAUSCHBETRAG,
    guenstigerpruefung,
    income_tax,
    tariff_for,
)


@pytest.mark.parametrize(
    ("year", "zve", "tax"),
    [
        # Zone edges and hand-computed points from the statutory formulas.
        (2026, 12348, 0),
        (2026, 17799, 1034),  # (914.51*0.5451 + 1400)*0.5451 = 1034.9
        (2026, 30000, 4217),  # (173.10*1.2201 + 2397)*1.2201 + 1034.87
        (2026, 100000, 30864),  # 0.42*100000 - 11135.63
        (2026, 300000, 115529),  # 0.45*300000 - 19470.38
        (2025, 12096, 0),
        (2025, 17443, 1015),
        (2024, 11784, 0),
        (2024, 50000, 10872),  # (181.19*3.2995 + 2397)*3.2995 + 991.21 = 10872.7
    ],
)
def test_tariff_matches_the_statute(year, zve, tax):
    assert income_tax(D(zve), year) == D(tax)


def test_tariff_is_continuous_at_the_zone_edges():
    for year in (2024, 2025, 2026):
        t, _ = tariff_for(year)
        for edge in (t.zone2_end, t.zone3_end):
            assert abs(income_tax(edge + 1, year) - income_tax(edge, year)) <= 2


def test_splitting_doubles_the_half():
    assert income_tax(D(60000), 2026, spouse=True) == 2 * income_tax(D(30000), 2026)


def test_a_student_owes_nothing_up_to_grundfreibetrag_plus_allowances():
    ceiling = D("12348") + D("1000") + SONDERAUSGABEN_PAUSCHBETRAG  # 13,384 €
    at = guenstigerpruefung(2026, capital_income_eur=ceiling)
    assert at.tariff_tax_eur == 0 and at.use_tariff and at.final_tax_eur == 0
    assert at.flat_tax_eur == D("3096.00")  # what a bank without an NV copy would take


def test_above_the_ceiling_the_tariff_still_beats_25_percent():
    g = guenstigerpruefung(2026, capital_income_eur=D("20000"))
    assert 0 < g.tariff_tax_eur < g.flat_tax_eur and g.use_tariff


def test_with_a_salary_the_flat_rate_wins():
    g = guenstigerpruefung(2026, capital_income_eur=D("5000"), other_income_eur=D("90000"))
    assert not g.use_tariff and g.final_tax_eur == g.flat_tax_eur == D("1000.00")


def test_unknown_year_falls_back_and_says_so():
    _, assumed = tariff_for(2030)
    assert assumed
    assert guenstigerpruefung(2030, capital_income_eur=D("1")).tariff_assumed
