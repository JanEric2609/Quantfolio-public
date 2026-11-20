"""§32d(1) EStG church-tax reduction of the effective Abgeltungsteuer (issue #140).

When the taxpayer is liable for church tax, church tax is deductible as a
Sonderausgabe, which lowers the effective capital-gains tax. The statutory
formula folds the foreign-WHT credit and the church-rate deduction together:

    KESt = (base - 4 * foreign_wht_creditable) / (4 + church_rate)

with church_rate in {0.08, 0.09}. With church_rate == 0 this reduces exactly to
the flat 25% KESt minus the foreign-WHT credit, so the non-church path is
unchanged.
"""
from decimal import Decimal

from app.foundation.tax_calc.jurisdictions.de.allowances import reduced_kest_amount


def test_church_reduces_flat_kest_no_foreign_wht():
    # base 10_000, church 9% -> 10000 / 4.09 = 2444.99 (< flat 2500)
    kest = reduced_kest_amount(Decimal("10000"), Decimal("0"), Decimal("0.09"))
    assert kest == Decimal("2444.99")
    assert kest < Decimal("2500")  # strictly below flat KESt


def test_church_with_foreign_wht_credit():
    # (10000 - 4*200) / 4.09 = 9200 / 4.09 = 2249.39
    kest = reduced_kest_amount(Decimal("10000"), Decimal("200"), Decimal("0.09"))
    assert kest == Decimal("2249.39")


def test_church_eight_percent_rate():
    # 10000 / 4.08 = 2450.98
    kest = reduced_kest_amount(Decimal("10000"), Decimal("0"), Decimal("0.08"))
    assert kest == Decimal("2450.98")


def test_zero_church_equals_flat_minus_foreign():
    # church_rate 0 -> (base - 4q)/4 == base*0.25 - q, byte-identical to old path.
    base, q = Decimal("10000"), Decimal("200")
    kest = reduced_kest_amount(base, q, Decimal("0"))
    flat_minus_foreign = (base * Decimal("0.25")).quantize(Decimal("0.01")) - q
    assert kest == flat_minus_foreign
    assert kest == Decimal("2300.00")


def test_never_negative():
    # foreign WHT exceeding the base must floor at zero, not go negative.
    kest = reduced_kest_amount(Decimal("100"), Decimal("1000"), Decimal("0.09"))
    assert kest == Decimal("0")
