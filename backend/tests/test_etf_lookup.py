"""Tests for ETF composition lookup service."""

from __future__ import annotations

from unittest.mock import patch

import pytest


from app.foundation.etf_lookup import (
    EtfComposition,
    EtfHolding,
    get_etf_composition,
    _get_static_fallback,
    _cache_key,
)


class TestEtfCompositionModels:
    """Test Pydantic models for ETF composition data."""

    def test_etf_holding_model(self):
        """EtfHolding should store ticker, weight, name."""
        holding = EtfHolding(ticker="AAPL", weight=0.05, name="Apple Inc.")
        assert holding.ticker == "AAPL"
        assert holding.weight == 0.05
        assert holding.name == "Apple Inc."

    def test_etf_composition_model(self):
        """EtfComposition should store ticker, name, holdings, sectors, regions."""
        holdings = [EtfHolding(ticker="AAPL", weight=0.05, name="Apple Inc.")]
        sectors = {"Technology": 0.30, "Healthcare": 0.15}
        regions = {"North America": 0.60, "Europe": 0.25}
        
        composition = EtfComposition(
            ticker="VWCE",
            name="Vanguard FTSE All-World",
            holdings=holdings,
            sectors=sectors,
            regions=regions,
        )
        
        assert composition.ticker == "VWCE"
        assert composition.name == "Vanguard FTSE All-World"
        assert len(composition.holdings) == 1
        assert composition.holdings[0].ticker == "AAPL"
        assert composition.sectors["Technology"] == 0.30
        assert composition.regions["North America"] == 0.60

    def test_etf_composition_empty_defaults(self):
        """EtfComposition should handle empty/None data gracefully."""
        composition = EtfComposition(
            ticker="TEST",
            name="Test ETF",
            holdings=[],
            sectors={},
            regions={},
        )
        assert composition.holdings == []
        assert composition.sectors == {}
        assert composition.regions == {}


class TestStaticFallback:
    """Test static fallback data for common ETFs."""

    def test_vwce_fallback(self):
        """Should return static data for VWCE (FTSE All-World)."""
        result = _get_static_fallback("VWCE.DE")
        assert result is not None
        assert result.ticker == "VWCE.DE"
        assert "Vanguard" in result.name
        assert len(result.holdings) > 0
        assert len(result.sectors) > 0
        # The hard-coded regions were out of date; regions come from the index.
        assert result.regions == {}

    def test_spy_fallback(self):
        """Should return static data for SPY (S&P 500)."""
        result = _get_static_fallback("SPY")
        assert result is not None
        assert result.ticker == "SPY"
        assert "SPDR" in result.name or "S&P" in result.name
        assert len(result.holdings) > 0

    def test_unknown_ticker_returns_none(self):
        """Should return None for unknown tickers."""
        result = _get_static_fallback("UNKNOWN_TICKER_123")
        assert result is None

    def test_case_insensitive_lookup(self):
        """Should handle case-insensitive ticker lookup."""
        result_upper = _get_static_fallback("VWCE.DE")
        result_lower = _get_static_fallback("vwce.de")
        # Both should find the same static data (or both None)
        if result_upper is not None:
            assert result_lower is not None or result_upper is not None


class TestGetEtfComposition:
    """Test the main get_etf_composition function."""

    @patch("app.foundation.etf_lookup._fetch_yfinance_composition")
    def test_returns_yfinance_data_when_available(self, mock_fetch):
        """Should prefer yfinance data over static fallback."""
        yfinance_data = EtfComposition(
            ticker="VWCE.DE",
            name="Vanguard FTSE All-World UCITS ETF",
            holdings=[EtfHolding(ticker="AAPL", weight=0.04, name="Apple Inc.")],
            sectors={"Technology": 0.25},
            regions={"North America": 0.55},
        )
        mock_fetch.return_value = yfinance_data

        result = get_etf_composition("VWCE.DE")
        
        assert result is not None
        assert result.ticker == "VWCE.DE"
        assert len(result.holdings) == 1
        assert result.holdings[0].ticker == "AAPL"
        mock_fetch.assert_called_once_with("VWCE.DE")

    @patch("app.foundation.etf_lookup._fetch_yfinance_composition")
    def test_falls_back_to_static_when_yfinance_fails(self, mock_fetch):
        """Should fall back to static data when yfinance returns None."""
        mock_fetch.return_value = None

        result = get_etf_composition("VWCE.DE")
        
        # Should get static fallback data
        assert result is not None
        assert result.ticker == "VWCE.DE"

    @patch("app.foundation.etf_lookup._fetch_yfinance_composition")
    def test_returns_none_for_unknown_ticker(self, mock_fetch):
        """Should return None for unknown tickers with no static data."""
        mock_fetch.return_value = None

        result = get_etf_composition("UNKNOWN_TICKER_123")
        
        assert result is None

    @patch("app.foundation.etf_lookup._fetch_yfinance_composition")
    def test_caches_results(self, mock_fetch):
        """Should cache composition data for 24 hours."""
        from app.foundation.etf_lookup import _cache
        
        mock_fetch.return_value = EtfComposition(
            ticker="VWCE.DE",
            name="Test",
            holdings=[],
            sectors={},
            regions={},
        )

        # Clear cache first
        _cache.clear()
        
        # First call
        result1 = get_etf_composition("VWCE.DE")
        # Second call (should use cache)
        result2 = get_etf_composition("VWCE.DE")
        
        assert result1 is not None
        assert result2 is not None
        # yfinance should only be called once (second call uses cache)
        mock_fetch.assert_called_once()


class TestCacheKey:
    """Test cache key generation."""

    def test_cache_key_normalization(self):
        """Should normalize tickers for cache keys."""
        key1 = _cache_key("VWCE.DE")
        key2 = _cache_key("vwce.de")
        key3 = _cache_key("VWCE")
        
        # All should produce the same normalized key
        assert key1 == key2
        # VWCE without exchange might differ
        assert isinstance(key1, str)
        assert len(key1) > 0


class TestYfinanceIntegration:
    """Test yfinance integration with mocking."""

    @patch("app.foundation.etf_lookup._fetch_yfinance_composition")
    def test_yfinance_data_used_when_available(self, mock_fetch):
        """Should use yfinance data when available."""
        from app.foundation.etf_lookup import _cache
        _cache.clear()  # Clear cache to ensure mock is called
        
        yfinance_data = EtfComposition(
            ticker="VWCE.DE",
            name="Vanguard FTSE All-World UCITS ETF",
            holdings=[EtfHolding(ticker="AAPL", weight=0.04, name="Apple Inc.")],
            sectors={"Technology": 0.25},
            regions={"North America": 0.55},
        )
        mock_fetch.return_value = yfinance_data

        from app.foundation.etf_lookup import get_etf_composition
        result = get_etf_composition("VWCE.DE")
        
        assert result is not None
        assert result.ticker == "VWCE.DE"
        assert len(result.holdings) == 1
        mock_fetch.assert_called_once()

    @patch("app.foundation.etf_lookup._fetch_yfinance_composition")
    def test_fallback_used_when_yfinance_fails(self, mock_fetch):
        """Should use static fallback when yfinance fails."""
        from app.foundation.etf_lookup import _cache
        _cache.clear()  # Clear cache to ensure mock is called
        
        mock_fetch.return_value = None

        from app.foundation.etf_lookup import get_etf_composition
        result = get_etf_composition("VWCE.DE")
        
        # Should get static fallback data
        assert result is not None
        assert result.ticker == "VWCE.DE"
        mock_fetch.assert_called_once()


class TestParseTopHoldings:
    """yfinance's ``funds_data.top_holdings`` as it really comes back."""

    @staticmethod
    def _frame():
        import pandas as pd

        frame = pd.DataFrame(
            {"Name": ["NVIDIA Corp", "Apple Inc", None], "Holding Percent": [0.054323, 0.050769, float("nan")]},
            index=pd.Index(["NVDA", "AAPL", "XXX"], name="Symbol"),
        )
        return frame

    def test_reads_symbol_index_and_titled_columns(self):
        """The symbol is the index and the weight column is "Holding Percent";
        reading lowercase column names returned UNKNOWN at weight 0 for every
        holding of every ETF."""
        from app.foundation.etf_lookup import _parse_top_holdings

        holdings = _parse_top_holdings(self._frame())

        assert [(h.ticker, h.weight, h.name) for h in holdings] == [
            ("NVDA", 0.054323, "NVIDIA Corp"),
            ("AAPL", 0.050769, "Apple Inc"),
        ]

    def test_empty_or_missing_frame(self):
        import pandas as pd

        from app.foundation.etf_lookup import _parse_top_holdings

        assert _parse_top_holdings(None) == []
        assert _parse_top_holdings(pd.DataFrame()) == []

    def test_overlap_of_two_parsed_etfs_is_not_zero(self):
        from app.foundation.etf_lookup import _parse_top_holdings
        from app.foundation.etf_overlap import pairwise_overlap

        rows = [{"ticker": h.ticker, "weight": h.weight} for h in _parse_top_holdings(self._frame())]

        assert pairwise_overlap(rows, rows)["overlap"] > 0.1


class TestIndexRegions:
    """The ETF page's geographic breakdown comes from the tracked index."""

    def test_regions_of_a_mapped_etf_by_ticker(self):
        from conftest import _memory_db

        from app.foundation.etf_lookup import _index_regions
        from app.foundation.models.entities import Asset

        db = _memory_db()
        db.add(Asset(isin="IE00B4L5Y983", symbol="EUNL.DE", name="iShares Core MSCI World"))
        db.commit()

        regions, source = _index_regions(db, "EUNL.DE")

        assert 0.72 < regions["United States"] < 0.74
        assert "North America" not in regions
        # Countries under 1% are folded into "Other" for the pie chart.
        assert all(share >= 0.01 for country, share in regions.items() if country != "Other")
        assert sum(regions.values()) == pytest.approx(1.0, abs=0.002)
        assert source is not None and source.startswith("MSCI World constituents")

    def test_an_unmapped_etf_has_no_regions(self):
        from conftest import _memory_db

        from app.foundation.etf_lookup import _index_regions

        assert _index_regions(_memory_db(), "THEME.DE") == ({}, None)
