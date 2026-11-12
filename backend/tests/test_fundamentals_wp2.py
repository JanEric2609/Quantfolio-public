"""WP2 (audit 2026-11-12): wrong or mislabelled fundamentals."""
from datetime import date

from app.foundation.fundamentals_context import (
    apply_range_checks,
    growth_basis_text,
    period_months_from_dates,
)
from app.foundation.providers.alphavantage_provider import _scaled
from app.foundation.providers.yfinance_provider import _fcf_yield, _fcf_yield_reason

BASE = {"freeCashflow": 2.0e9, "marketCap": 10.0e9, "currency": "EUR", "financialCurrency": "EUR",
        "sector": "Industrials", "industry": "Machinery"}


def test_clean_fcf_yield_is_cash_flow_over_market_cap():
    assert _fcf_yield(BASE) == 0.2


def test_pence_quote_has_no_fcf_yield():
    assert _fcf_yield({**BASE, "currency": "GBp", "financialCurrency": "GBP"}) is None
    assert _fcf_yield_reason({**BASE, "currency": "GBp"}) == "currency_mismatch"


def test_mismatched_reporting_currency_has_no_fcf_yield():
    assert _fcf_yield({**BASE, "currency": "EUR", "financialCurrency": "USD"}) is None


def test_bank_has_no_fcf_yield():
    info = {**BASE, "sector": "Financial Services", "industry": "Banks - Regional"}
    assert _fcf_yield(info) is None and _fcf_yield_reason(info) == "financial_sector"


def test_range_check_nulls_implausible_yields_with_a_reason():
    out = apply_range_checks({"fcf_yield": 1.2, "dividend_yield": 0.15, "pe_ratio": 9.0})
    assert out["fcf_yield"] is None and out["fcf_yield_reason"] == "out_of_range"
    assert out["dividend_yield"] is None and out["dividend_yield_reason"] == "out_of_range"
    ok = apply_range_checks({"fcf_yield": 0.05, "dividend_yield": 0.04})
    assert ok["fcf_yield"] == 0.05 and "fcf_yield_reason" not in ok


def test_period_months_from_statement_dates():
    assert period_months_from_dates([date(2026, 6, 30), date(2025, 12, 31)]) == 6
    assert period_months_from_dates([date(2026, 9, 30), date(2026, 6, 30)]) == 3
    assert period_months_from_dates(["2025-12-31", "2024-12-31"]) == 12
    assert period_months_from_dates([date(2026, 6, 30)]) is None


def test_growth_label_names_the_half_year():
    assert "half-year" in growth_basis_text("quarter_yoy", 6)
    assert "quarter" in growth_basis_text("quarter_yoy", 3)
    assert "quarter" not in growth_basis_text("quarter_yoy", None) or "latest quarter" in growth_basis_text("quarter_yoy", None)


def test_alpha_vantage_debt_equity_ratio_becomes_percent():
    assert _scaled(1.5, 100.0) == 150.0 and _scaled(None, 100.0) is None
