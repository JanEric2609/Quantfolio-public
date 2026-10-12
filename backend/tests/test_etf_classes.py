"""ETF asset class and index key (plan 2026-10-12, WP4a)."""
from __future__ import annotations

import pytest

from app.decision.discover.universe import _FIXED_INCOME_SEED
from app.foundation.etf_classes import (
    ESTR_BENCHMARK,
    benchmark_symbol_for,
    classify_etf,
    index_key,
)

# Names as justETF lists them (the top of the 2026-10 overview).
CASES = [
    ("iShares Core MSCI World UCITS ETF USD (Acc)", "equity_broad", "MSCI World"),
    ("Xtrackers MSCI World UCITS ETF 1C", "equity_broad", "MSCI World"),
    ("Vanguard FTSE All-World UCITS ETF (USD) Accumulating", "equity_broad", "FTSE All-World"),
    ("iShares MSCI All Country World UCITS ETF USD (Acc)", "equity_broad", "MSCI ACWI"),
    ("iShares Core S&P 500 UCITS ETF USD (Acc)", "equity_region", "S&P 500"),
    ("Vanguard S&P 500 UCITS ETF (USD) Distributing", "equity_region", "S&P 500"),
    ("Invesco S&P 500 UCITS ETF", "equity_region", "S&P 500"),
    ("iShares Core MSCI Emerging Markets IMI UCITS ETF (Acc)", "equity_region", "MSCI EM IMI"),
    ("iShares Core FTSE 100 UCITS ETF GBP (Dist)", "equity_region", "FTSE 100"),
    ("Amundi Core Stoxx Europe 600 UCITS ETF Acc", "equity_region", "STOXX Europe 600"),
    ("iShares Nasdaq 100 UCITS ETF (Acc)", "equity_region", "Nasdaq-100"),
    ("iShares Edge MSCI World Value Factor UCITS ETF", "equity_factor", "MSCI World Value"),
    ("Xtrackers MSCI World Momentum UCITS ETF 1C", "equity_factor", "MSCI World Momentum"),
    ("Xtrackers S&P 500 Equal Weight UCITS ETF 1C", "equity_factor", "S&P 500 Equal Weight"),
    ("iShares S&P 500 Information Technology Sector UCITS ETF USD (Acc)", "equity_sector_theme",
     "S&P 500 Information Technology Sector"),
    ("VanEck Semiconductor UCITS ETF", "equity_sector_theme", "Semiconductor"),
    ("iShares Euro Government Bond 1-3yr UCITS ETF (Acc)", "bond_govt_eur_short", "Euro Govt 1-3Y"),
    ("Amundi Euro Government Bond 1-3Y UCITS ETF Acc", "bond_govt_eur_short", "Euro Govt 1-3Y"),
    ("iShares Euro Government Bond 3-5yr UCITS ETF", "bond_govt_eur_mid", "Euro Govt 3-5Y"),
    ("Xtrackers II Eurozone Government Bond UCITS ETF 1C", "bond_govt_eur_mid", "Euro Govt"),
    ("Vanguard Global Aggregate Bond UCITS ETF EUR Hedged Accumulating", "bond_aggregate_eur_hedged",
     "Bloomberg Global Aggregate (EUR hedged)"),
    ("iShares Core EUR Corporate Bond UCITS ETF (Dist)", "bond_other", None),
    ("Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C", "money_market", "€STR"),
    ("Amundi EUR Overnight Return UCITS ETF Acc", "money_market", "€STR"),
    ("Amundi Smart Overnight Return UCITS ETF USD Hedged Acc", "bond_other", None),
    ("iShares Physical Gold ETC", "gold", "Gold"),
    ("Xetra-Gold", "gold", "Gold"),
    ("VanEck Gold Miners UCITS ETF", "equity_sector_theme", None),
    ("iShares Physical Silver ETC", "commodity_other", None),
    ("WisdomTree NASDAQ 100 3x Daily Leveraged", "leveraged_inverse", None),
    ("Xtrackers S&P 500 Inverse Daily Swap UCITS ETF 1C", "leveraged_inverse", None),
]


@pytest.mark.parametrize("name,etf_class,key", CASES)
def test_class_and_index(name, etf_class, key):
    assert classify_etf(name) == etf_class
    if key is not None:
        assert index_key(name) == key


def test_fixed_income_seed_is_short_euro_govt():
    for etf in _FIXED_INCOME_SEED:
        assert classify_etf(etf["name"], isin=etf["isin"]) == "bond_govt_eur_short"


def test_broad_name_inside_a_factor_fund_is_its_own_index():
    assert index_key("Vanguard FTSE All-World High Dividend Yield UCITS ETF") != "FTSE All-World"


def test_benchmarks_by_class():
    assert benchmark_symbol_for("equity_region") == "EUNL.DE"
    assert benchmark_symbol_for("equity_sector_theme") == "EUNL.DE"
    assert benchmark_symbol_for("money_market") == ESTR_BENCHMARK
    assert benchmark_symbol_for("gold") is None
    assert benchmark_symbol_for("bond_govt_eur_short") == "CBE3.L"
    for bond in ("bond_govt_eur_short", "bond_govt_eur_mid", "bond_govt_eur_long", "bond_aggregate_eur_hedged",
                 "bond_other"):
        assert benchmark_symbol_for(bond) not in ("EUNL.DE", "URTH", "SPY", "VGK", "EWJ")
