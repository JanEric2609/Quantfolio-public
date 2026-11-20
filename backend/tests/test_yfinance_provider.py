"""Tests for app.foundation.providers.yfinance_provider.

Pure-function coverage for _dividend_yield_fraction (dividend_yield was
never populated in get_fundamentals' output dict — see
docs/archive/audits/2026-08-26/deepdive-01-discover-engine.md Bug 2). No DB or
network access needed for these cases.
"""

from app.foundation.providers.yfinance_provider import _dividend_yield_fraction


class TestDividendYieldFraction:
    """yfinance >= 0.2.54 reports ``dividendYield`` in percent."""

    def test_percent_value_becomes_fraction(self) -> None:
        # BBVA.MC's ~3.68% dividend yield.
        result = _dividend_yield_fraction({"dividendYield": 3.68})
        assert result is not None
        assert abs(result - 0.0368) < 1e-9

    def test_sub_half_percent_yield_is_not_taken_as_a_fraction(self) -> None:
        # AAPL on prod, 2026-09-26: 0.32 means 0.32%, not 32%.
        result = _dividend_yield_fraction({"dividendYield": 0.32})
        assert result is not None
        assert abs(result - 0.0032) < 1e-9

    def test_missing_key_returns_none(self) -> None:
        info: dict = {}
        assert _dividend_yield_fraction(info) is None

    def test_falls_back_to_trailing_annual_dividend_yield(self) -> None:
        # trailingAnnualDividendYield is still a fraction.
        info = {"dividendYield": None, "trailingAnnualDividendYield": 0.021}
        assert _dividend_yield_fraction(info) == 0.021

    def test_dividend_yield_takes_priority_over_fallback(self) -> None:
        info = {"dividendYield": 5.0, "trailingAnnualDividendYield": 0.02}
        assert abs(_dividend_yield_fraction(info) - 0.05) < 1e-9

    def test_both_missing_returns_none(self) -> None:
        info = {"dividendYield": None, "trailingAnnualDividendYield": None}
        assert _dividend_yield_fraction(info) is None

    def test_non_numeric_value_returns_none(self) -> None:
        info = {"dividendYield": "not-a-number"}
        assert _dividend_yield_fraction(info) is None


def test_get_fundamentals_includes_dividend_yield_key(monkeypatch) -> None:
    """get_fundamentals' output dict now carries dividend_yield, wired from
    the same yfinance .info payload used for every other fundamentals field."""
    from app.foundation.providers.yfinance_provider import YFinanceProvider

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.info = {
                "trailingPE": 12.0,
                "dividendYield": 3.68,
            }

    import app.foundation.providers.yfinance_provider as mod

    class _FakeYf:
        Ticker = _FakeTicker

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _FakeYf())

    provider = mod.YFinanceProvider()
    result = provider.get_fundamentals("BBVA.MC")

    assert result["ok"] is True
    data = result["data"]
    assert "dividend_yield" in data
    assert abs(data["dividend_yield"] - 0.0368) < 1e-9


def test_get_history_carries_the_quote_currency_from_the_response_metadata(monkeypatch) -> None:
    """The chart response names the unit of the bars it returns ("GBp" for
    SHEL.L, "USD" for IWDA.L on the LSE); it used to be dropped, so every
    stored bar got a guessed label."""
    import pandas as pd

    import app.foundation.providers.yfinance_provider as mod

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.history_metadata: dict = {}

        def history(self, **_kw):
            self.history_metadata = {"currency": "GBp"}
            idx = pd.to_datetime(["2026-09-24", "2026-09-25"])
            return pd.DataFrame({"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
                                 "Close": [3600.0, 3611.0], "Volume": [10, 20]}, index=idx)

    class _FakeYf:
        Ticker = _FakeTicker

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _FakeYf())

    result = mod.YFinanceProvider().get_history("SHEL.L", days=5)

    assert result["ok"] is True
    assert [row["currency"] for row in result["data"]] == ["GBp", "GBp"]
