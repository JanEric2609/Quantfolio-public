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


def test_pence_quote_is_read_in_pounds():
    # TSCO.L on 2026-11-12: 552p, market cap 34.2 bn GBP (Yahoo states it in pounds), FCF 2.13 bn GBP.
    tsco = {**BASE, "currency": "GBp", "financialCurrency": "GBP", "freeCashflow": 2.131e9, "marketCap": 34.2035e9}
    assert abs(_fcf_yield(tsco) - 0.0623) < 1e-4 and _fcf_yield_reason(tsco) is None


def test_mismatched_reporting_currency_is_converted_or_left_out():
    # ULVR.L: reports in EUR, quoted in pence. 6.73 bn EUR at 0.87 GBP per EUR over 106.1 bn GBP = 5.5 %.
    ulvr = {**BASE, "currency": "GBp", "financialCurrency": "EUR", "freeCashflow": 6.7326e9, "marketCap": 106.1287e9}
    assert _fcf_yield(ulvr) is None and _fcf_yield_reason(ulvr) == "currency_mismatch"  # no rate: n/a
    assert abs(_fcf_yield(ulvr, fx_rate=0.87) - 6.7326 * 0.87 / 106.1287) < 1e-9
    assert _fcf_yield_reason(ulvr, fx_rate=0.87) is None
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


class _Sheet:
    """A ticker stand-in whose ``balance_sheet`` is the given frame."""

    def __init__(self, rows: dict[str, float] | None):
        import pandas as pd

        self.balance_sheet = pd.DataFrame({"2025-12-31": rows}) if rows is not None else pd.DataFrame()


def test_minority_share_is_minority_over_group_equity():
    from app.foundation.providers.yfinance_provider import _large_minorities, _minority_share

    # DTE.DE's annual sheet of 2025 (EUR): 30,061 m of 92,232 m group equity is minorities' (32.6 %).
    dte = _Sheet({"Minority Interest": 30_061e6, "Total Equity Gross Minority Interest": 92_232e6})
    assert abs(_minority_share(dte) - 0.32593) < 1e-4
    assert _large_minorities(_minority_share(dte))
    # WKL.AS: no minorities.
    wkl = _Sheet({"Minority Interest": 0.0, "Total Equity Gross Minority Interest": 798e6})
    assert _minority_share(wkl) == 0.0 and not _large_minorities(0.0)


def test_minority_share_without_the_row_is_zero_and_without_a_sheet_unknown():
    from app.foundation.providers.yfinance_provider import _large_minorities, _minority_share

    assert _minority_share(_Sheet({"Stockholders Equity": 6_206e6})) == 0.0
    assert _minority_share(_Sheet(None)) is None and not _large_minorities(None)
    # Falls back to parent equity plus minorities when the gross total is missing: 20 / (60 + 20) = 25 %, not above.
    assert _minority_share(_Sheet({"Minority Interest": 20.0, "Stockholders Equity": 60.0})) == 0.25
    assert not _large_minorities(0.25)


def test_period_falls_back_to_the_balance_sheet_dates():
    import pandas as pd

    from app.foundation.providers.yfinance_provider import _statement_period_months

    class _HalfYear:  # WKL.AS on 2026-11-12: no quarterly income statement, half-year balance sheets
        quarterly_income_stmt = pd.DataFrame()
        quarterly_balance_sheet = pd.DataFrame(columns=pd.to_datetime(["2026-06-30", "2025-12-31", "2025-06-30"]))
        quarterly_cashflow = pd.DataFrame()

    assert _statement_period_months(_HalfYear()) == 6

    class _Quarterly(_HalfYear):
        quarterly_income_stmt = pd.DataFrame(columns=pd.to_datetime(["2026-09-30", "2026-06-30"]))

    assert _statement_period_months(_Quarterly()) == 3

    class _Nothing:
        quarterly_income_stmt = quarterly_balance_sheet = quarterly_cashflow = pd.DataFrame()

    assert _statement_period_months(_Nothing()) is None
