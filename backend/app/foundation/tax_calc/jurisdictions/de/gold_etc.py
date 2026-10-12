"""Gold ETCs in German tax (plan 2026-10-12, D4). Estimates, not tax advice.

A gold ETC such as Xetra-Gold is a bearer bond that entitles the holder to
delivery of physical gold. The BFH held that it is not a capital claim, so
its gains are not investment income under § 20 EStG (BFH 12.05.2015,
VIII R 35/14 and VIII R 4/15), also when the holder may ask for the cash
proceeds instead of the gold (BFH 16.06.2020, VIII R 7/17). It is not an
investment fund either, so the InvStG does not apply: no Vorabpauschale and
no Teilfreistellung. A sale is a private sale under § 23 Abs. 1 Nr. 2 EStG:
a gain is taxable at the personal income-tax rate within one year of
buying and tax-free after it.

Pure: no DB, no network.
"""
from __future__ import annotations

from datetime import date
from typing import Any

#: Physically backed gold ETCs with a deliverable-gold claim (issuer documents).
GOLD_ETC_ISINS: frozenset[str] = frozenset({
    "DE000A0S9GB0",  # Xetra-Gold
    "DE000A1E0HR8",  # EUWAX Gold II
})
_NAME_MARKERS = ("xetra-gold", "xetra gold", "euwax gold")

#: § 23 Abs. 1 Nr. 2 EStG: the holding period after which a gain is tax-free.
HOLDING_PERIOD_DAYS = 365

TAX_NOTE = (
    "Gold ETC: not an investment fund, so no Vorabpauschale and no Teilfreistellung. A sale is a "
    "private sale (§ 23 EStG): a gain is taxable within 12 months of buying and tax-free after "
    "(BFH 12.05.2015, VIII R 35/14 and VIII R 4/15; BFH 16.06.2020, VIII R 7/17)."
)


def is_gold_etc(isin: str | None, name: str | None = None) -> bool:
    if (isin or "").upper() in GOLD_ETC_ISINS:
        return True
    n = (name or "").lower()
    return any(m in n for m in _NAME_MARKERS)


def _one_year_later(bought: date) -> date:
    try:
        return bought.replace(year=bought.year + 1)
    except ValueError:  # 29 February
        return bought.replace(year=bought.year + 1, day=28)


def holding_status(bought: date | None, today: date) -> dict[str, Any]:
    """When a gold ETC lot becomes tax-free on sale (§ 23 EStG), as an estimate."""
    out: dict[str, Any] = {"tax_rule": "§ 23 EStG", "note": TAX_NOTE, "estimate": True, "not_tax_advice": True}
    if bought is None:
        out.update({"tax_free_from": None, "tax_free_now": None})
        return out
    # The period ends one year after the purchase; a sale the day after is outside it.
    free_from = date.fromordinal(_one_year_later(bought).toordinal() + 1)
    out.update({"tax_free_from": free_from.isoformat(), "tax_free_now": today >= free_from})
    return out
