"""Gold ETC tax treatment (plan 2026-10-12, D4): § 23 EStG, no Vorabpauschale."""
from __future__ import annotations

from datetime import date

from app.foundation.tax_calc.jurisdictions.de.gold_etc import holding_status, is_gold_etc


def test_xetra_gold_is_a_gold_etc_by_isin_or_name():
    assert is_gold_etc("DE000A0S9GB0")
    assert is_gold_etc(None, "Xetra-Gold")
    assert not is_gold_etc("IE00B4L5Y983", "iShares Core MSCI World")


def test_tax_free_after_one_year():
    s = holding_status(date(2025, 10, 12), date(2026, 10, 12))
    assert s["estimate"] is True and s["not_tax_advice"] is True
    assert s["tax_rule"] == "§ 23 EStG"
    assert s["tax_free_from"] == "2026-10-13"
    assert s["tax_free_now"] is False
    assert holding_status(date(2025, 10, 12), date(2026, 10, 13))["tax_free_now"] is True


def test_leap_day_purchase():
    assert holding_status(date(2024, 2, 29), date(2025, 3, 1))["tax_free_from"] == "2025-03-01"


def test_unknown_purchase_date():
    s = holding_status(None, date(2026, 10, 12))
    assert s["tax_free_from"] is None and s["estimate"] is True and s["not_tax_advice"] is True
