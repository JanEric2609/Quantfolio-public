"""Tests for etf_universe.py's fund-class keyword detection.

Regression coverage for the XEON.DE prod incident (2026-08-20): the
money-market keyword list was missing overnight-rate/€STR/EONIA-style
terms, so a money-market ETF like XEON.DE (Xtrackers II EUR Overnight
Rate Swap UCITS ETF) fell through to the default "aktien" (30% German
Teilfreistellung) tax classification instead of "other".
"""
from __future__ import annotations

from app.foundation.etf_universe import _derive_tf_class, is_bond_fund, is_money_market_fund


class TestIsMoneyMarketFund:
    def test_xeon_de_detected(self):
        assert is_money_market_fund("Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C") is True

    def test_estr_variants_detected(self):
        assert is_money_market_fund("Some ESTR Cash Fund") is True
        assert is_money_market_fund("Amundi EONIA UCITS ETF") is True
        assert is_money_market_fund("iShares SONIA Overnight Rate ETF") is True

    def test_equity_etf_not_detected(self):
        assert is_money_market_fund("Vanguard FTSE All-World UCITS ETF") is False
        assert is_money_market_fund("iShares Core MSCI World UCITS ETF") is False

    def test_equity_swap_replication_not_falsely_flagged(self):
        """Synthetic-replication equity index ETFs also use "swap" in some
        descriptions — must not be caught by the money-market keywords."""
        assert is_money_market_fund("Amundi MSCI World Swap UCITS ETF") is False


class TestIsBondFund:
    def test_cbe3_l_detected(self):
        """F17: iShares € Govt Bond 1-3yr UCITS ETF (CBE3.L)."""
        assert is_bond_fund("iShares € Govt Bond 1-3yr UCITS ETF (Acc)") is True

    def test_various_bond_keywords_detected(self):
        assert is_bond_fund("iShares Core € Corp Bond UCITS ETF") is True
        assert is_bond_fund("Xtrackers II Eurozone Government Bond UCITS ETF") is True
        assert is_bond_fund("iShares $ Treasury Bond 1-3yr UCITS ETF") is True

    def test_equity_etf_not_detected(self):
        assert is_bond_fund("Vanguard FTSE All-World UCITS ETF") is False

    def test_money_market_fund_not_detected_as_bond(self):
        """Disjoint from is_money_market_fund — a money-market fund must not
        also match the bond keyword set."""
        assert is_bond_fund("Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C") is False


class TestDeriveTfClass:
    def test_money_market_fund_classified_as_other(self):
        assert _derive_tf_class("Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C", None) == "other"

    def test_equity_etf_classified_as_aktien(self):
        assert _derive_tf_class("Vanguard FTSE All-World UCITS ETF", None) == "aktien"

    def test_bond_fund_classified_as_other(self):
        assert _derive_tf_class("iShares Core Euro Government Bond UCITS ETF", None) == "other"


def test_cache_write_leaves_a_whole_file_and_no_temporary(tmp_path, monkeypatch):
    """The cache is written beside the target and renamed over it, so a
    reader never sees a half-written file."""
    import json

    from app.foundation import etf_universe

    monkeypatch.setattr(etf_universe, "_CACHE_DIR", str(tmp_path))
    provider = etf_universe.EtfUniverseProvider()
    provider._save_cache([{"isin": "IE00B4L5Y983"}])
    provider._save_cache([{"isin": "IE00B5BMR087"}])

    assert [p.name for p in tmp_path.iterdir()] == ["etf_overview.json"]
    assert json.loads((tmp_path / "etf_overview.json").read_text()) == [{"isin": "IE00B5BMR087"}]
    assert provider.get_universe_cached() == [{"isin": "IE00B5BMR087"}]
