"""Foreign withholding tax helpers.

The German DBA framework typically caps creditable foreign WHT at 15% of the
gross dividend for treaty countries. Anything above that requires reclaim from
the source country. We model the cap; the user is responsible for reclaim.

Per-country caps are maintained in ``TREATY_CAP_BY_COUNTRY`` — a periodic
maintenance table (like Basiszins / Teilfreistellung). The source is the BMF
DBA-Übersicht (Doppelbesteuerungsabkommen). **This is an estimate** — always
verify against the current DBA text for the specific country and year.
"""
from __future__ import annotations

from decimal import Decimal


DEFAULT_TREATY_CAP_PCT = Decimal("0.15")

# German DBA treaty caps for creditable foreign withholding tax.
# Most treaties cap at 15 % ; a few have lower rates.
# Source: BMF DBA-Übersicht, § 4.2 InvStG 2018.
# NOTE: This is an estimate — verify against the current DBA text.
TREATY_CAP_BY_COUNTRY: dict[str, Decimal] = {
    "US": Decimal("0.15"),
    "CH": Decimal("0.15"),
    "GB": Decimal("0.15"),
    "JP": Decimal("0.15"),
    "FR": Decimal("0.15"),
    "NL": Decimal("0.15"),
    "CA": Decimal("0.15"),
    "IT": Decimal("0.15"),
    "ES": Decimal("0.15"),
    "SE": Decimal("0.15"),
    "DK": Decimal("0.15"),
    "NO": Decimal("0.15"),
    "AT": Decimal("0.15"),
    "IE": Decimal("0.10"),
}

TREATY_CAP_NOTICE = (
    "Per-country DBA treaty caps are estimates — verify against the current "
    "BMF DBA-Übersicht for the specific country and tax year."
)


def cap_foreign_wht(
    foreign_wht_eur: Decimal,
    gross_dividend_eur: Decimal,
    treaty_cap_pct: Decimal = DEFAULT_TREATY_CAP_PCT,
    country: str | None = None,
) -> dict:
    """Return creditable vs. excess foreign WHT.

    Anrechenbar (creditable against KESt) is min(foreign_wht, gross * cap).

    When *country* is provided the cap is looked up from ``TREATY_CAP_BY_COUNTRY``
    (case-insensitive), falling back to *treaty_cap_pct* for unknown countries.
    """
    if country is not None:
        treaty_cap_pct = TREATY_CAP_BY_COUNTRY.get(
            country.upper(), treaty_cap_pct
        )
    if gross_dividend_eur <= 0:
        return {
            "creditable_eur": float(Decimal("0")),
            "excess_eur": float(foreign_wht_eur),
            "treaty_cap_pct": float(treaty_cap_pct),
        }
    cap_amount = (gross_dividend_eur * treaty_cap_pct).quantize(Decimal("0.01"))
    creditable = min(foreign_wht_eur, cap_amount)
    excess = max(Decimal("0"), foreign_wht_eur - creditable)
    return {
        "creditable_eur": float(creditable.quantize(Decimal("0.01"))),
        "excess_eur": float(excess.quantize(Decimal("0.01"))),
        "treaty_cap_pct": float(treaty_cap_pct),
        "notice": TREATY_CAP_NOTICE,
    }
