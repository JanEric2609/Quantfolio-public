"""Finnhub fundamentals are restated into yfinance's units.

Finnhub answers first for US listings, but reports ratios in percent, market
cap in millions and volumes in millions of shares. Every consumer reads
yfinance's units, so unconverted payloads dropped AAPL out of the Discover
universe (4.9M "market cap" < the 250M floor) and maxed the fundamentals
score's ROE and growth terms for every US name (prod, 2026-09-26). The raw
values below are AAPL's live Finnhub payload from that day.
"""
from __future__ import annotations

import pytest

from app.foundation.providers.finnhub_provider import FinnhubProvider

_METRIC = {
    "peNormalizedAnnual": 44.3089, "pbAnnual": 50.978, "roeTTM": 137.18,
    "roiTTM": 70.25, "netProfitMarginTTM": 27.62, "grossMarginTTM": 48.65,
    "totalDebt/totalEquityAnnual": 1.3547, "revenueGrowthTTMYoy": 14.24,
    "epsGrowthTTMYoy": 32.61, "beta": 1.0921, "10DayAverageTradingVolume": 41.31066,
    "3MonthAverageTradingVolume": 51.87837, "currentDividendYieldTTM": 0.3151,
}
_PROFILE = {"marketCapitalization": 4977637.06, "currency": "USD", "country": "US", "finnhubIndustry": "Technology"}


@pytest.fixture
def aapl(monkeypatch):
    provider = FinnhubProvider(api_key="k")

    def _fake_get(path, params):
        return {"metric": _METRIC} if path == "/stock/metric" else _PROFILE

    monkeypatch.setattr(provider, "_get", _fake_get)
    result = provider.get_fundamentals("AAPL")
    assert result["ok"] is True
    return result["data"]


def test_market_cap_and_volume_are_absolute(aapl):
    assert aapl["market_cap"] == pytest.approx(4.97763706e12)
    assert aapl["volume"] == pytest.approx(41_310_660)
    assert aapl["average_volume"] == pytest.approx(51_878_370)


def test_ratios_are_fractions_like_yfinance(aapl):
    # yfinance for AAPL the same day: returnOnEquity 1.4875, profitMargins
    # 0.2762, revenueGrowth 0.164, grossMargins 0.4865, dividendYield 0.32%.
    assert aapl["roe"] == pytest.approx(1.3718)
    assert aapl["roic"] == pytest.approx(0.7025)
    assert aapl["profit_margin"] == pytest.approx(0.2762)
    assert aapl["gross_margin"] == pytest.approx(0.4865)
    assert aapl["revenue_growth"] == pytest.approx(0.1424)
    assert aapl["earnings_growth"] == pytest.approx(0.3261)
    assert aapl["dividend_yield"] == pytest.approx(0.003151)


def test_debt_equity_is_percent_like_yfinance(aapl):
    # yfinance's debtToEquity is in percent (78.4 for 0.784x).
    assert aapl["debt_equity"] == pytest.approx(135.47)


def test_passes_the_discover_universe_floors(aapl):
    from app.decision.discover.config import DEFAULT_UNIVERSE_CONFIG

    assert aapl["market_cap"] >= DEFAULT_UNIVERSE_CONFIG["min_market_cap_eur"]
    assert aapl["average_volume"] >= DEFAULT_UNIVERSE_CONFIG["min_avg_volume"]


def test_missing_fields_stay_none(monkeypatch):
    provider = FinnhubProvider(api_key="k")
    monkeypatch.setattr(provider, "_get", lambda path, params: {"metric": {}} if path == "/stock/metric" else {"currency": "USD"})
    data = provider.get_fundamentals("AAPL")["data"]
    assert data["market_cap"] is None
    assert data["roe"] is None
    assert data["volume"] is None


def _fundamentals(monkeypatch, metric):
    provider = FinnhubProvider(api_key="k")
    monkeypatch.setattr(provider, "_get", lambda path, params: {"metric": metric} if path == "/stock/metric" else _PROFILE)
    return provider.get_fundamentals("MU")["data"]


def test_valuation_uses_current_ttm_ratios_not_last_fiscal_year(monkeypatch):
    # MU's live payload on 2026-09-28: a year of 7x EPS growth left the
    # annual fields at P/E 143 (FY2025 EPS) and P/B 2.5 (FY2025 price).
    data = _fundamentals(monkeypatch, {
        "peTTM": 24.2192, "peNormalizedAnnual": 143.1455,
        "pb": 12.1353, "pbQuarterly": 10.34, "pbAnnual": 2.5207,
        "totalDebt/totalEquityQuarterly": 0.0568, "totalDebt/totalEquityAnnual": 0.2691,
    })
    assert data["pe_ratio"] == pytest.approx(24.2192)
    assert data["pb_ratio"] == pytest.approx(12.1353)
    assert data["debt_equity"] == pytest.approx(5.68)


def test_annual_ratios_remain_the_fallback(monkeypatch):
    data = _fundamentals(monkeypatch, {"peNormalizedAnnual": 44.3, "pbAnnual": 50.9, "totalDebt/totalEquityAnnual": 1.35})
    assert data["pe_ratio"] == pytest.approx(44.3)
    assert data["pb_ratio"] == pytest.approx(50.9)
    assert data["debt_equity"] == pytest.approx(135.0)


def test_a_loss_is_not_a_low_pe(monkeypatch):
    data = _fundamentals(monkeypatch, {"peTTM": -12.0, "pb": -3.0})
    assert data["pe_ratio"] is None
    assert data["pb_ratio"] is None
