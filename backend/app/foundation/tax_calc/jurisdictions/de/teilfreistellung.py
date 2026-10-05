"""ETF/fund Teilfreistellung per §20 InvStG 2018."""
from __future__ import annotations

from decimal import Decimal


# §20 InvStG 2018 — partial exemption rates for private investors.
TEILFREISTELLUNG_RATES: dict[str, Decimal] = {
    "aktien": Decimal("0.30"),       # ≥51% equity
    "misch": Decimal("0.15"),        # ≥25% equity
    "immobilien": Decimal("0.60"),   # ≥51% real estate (DE)
    "immobilien_foreign": Decimal("0.80"),
    "other": Decimal("0"),
}


def teilfreistellung_pct_for_fund_class(fund_class: str | None) -> Decimal:
    if not fund_class:
        return Decimal("0")
    return TEILFREISTELLUNG_RATES.get(fund_class.lower(), Decimal("0"))


def apply_teilfreistellung(gross_eur: Decimal, teilfreistellung_pct: Decimal) -> Decimal:
    """Return the taxable portion after Teilfreistellung."""
    if teilfreistellung_pct <= 0:
        return gross_eur
    factor = Decimal("1") - teilfreistellung_pct
    return (gross_eur * factor).quantize(Decimal("0.01"))
