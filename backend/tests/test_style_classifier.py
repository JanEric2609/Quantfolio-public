"""Tests for the deterministic growth/value style classifier (ADR 0014 §6).

Primary regression cases are the five dossiers ADR 0014 found the LLM
mis-describing, using their actual top-10 holdings as recorded in the ADR.
"""
from app.decision.discover.style_classifier import classify_style

# IS3S.DE — MSCI World Value Factor ETF; the ADR's disputed "described as
# growth" ticker. Top-10: Micron, Cisco, Verizon, Toyota, AT&T.
_IS3S_HOLDINGS = [
    {"ticker": "MU", "name": "Micron Technology"},
    {"ticker": "CSCO", "name": "Cisco Systems"},
    {"ticker": "VZ", "name": "Verizon Communications"},
    {"ticker": "TM", "name": "Toyota Motor"},
    {"ticker": "T", "name": "AT&T"},
]

# XAIX.DE — an AI/tech growth ETF. Top-10: Microsoft, Amazon, Apple, NVIDIA,
# Alphabet.
_XAIX_HOLDINGS = [
    {"ticker": "MSFT", "name": "Microsoft"},
    {"ticker": "AMZN", "name": "Amazon"},
    {"ticker": "AAPL", "name": "Apple"},
    {"ticker": "NVDA", "name": "NVIDIA"},
    {"ticker": "GOOGL", "name": "Alphabet"},
]


def test_index_name_value_mandate_wins_regardless_of_holdings():
    """IS3S.DE's index name states its mandate explicitly — this is the
    strongest signal and must not be second-guessed by holdings."""
    result = classify_style("etf", holdings=_IS3S_HOLDINGS, index_name="MSCI World Value Factor")
    assert result["style"] == "value"
    assert "index_name" in result["basis"]


def test_holdings_majority_growth_without_index_name():
    """XAIX.DE-style top-10 (mega-cap tech/AI names) classifies growth from
    holdings alone when no explicit mandate is available."""
    result = classify_style("etf", holdings=_XAIX_HOLDINGS, index_name=None)
    assert result["style"] == "growth"
    assert result["basis"] == "top_10_holdings_majority"


def test_holdings_majority_value_without_index_name():
    result = classify_style("etf", holdings=_IS3S_HOLDINGS, index_name=None)
    assert result["style"] == "value"


def test_index_name_growth_keyword():
    result = classify_style("etf", holdings=None, index_name="Nasdaq-100 Growth Index")
    assert result["style"] == "growth"


def test_no_evidence_never_guesses():
    """Mixed/unknown holdings and no index name must return None, not a
    guessed style — this is the exact defect the ADR flags: the LLM should
    never be left to fill a gap this classifier declined to fill."""
    result = classify_style(
        "etf",
        holdings=[{"ticker": "XXXX", "name": "Unknown Co"}],
        index_name=None,
    )
    assert result["style"] is None
    assert result["basis"] == "insufficient_evidence"


def test_no_holdings_no_index_name_never_guesses():
    result = classify_style("etf", holdings=None, index_name=None)
    assert result["style"] is None


def test_equities_are_not_applicable():
    result = classify_style("equity", holdings=_XAIX_HOLDINGS, index_name="Value")
    assert result["style"] is None
    assert result["basis"] == "not_applicable_to_equities"


def test_minority_holdings_cluster_does_not_classify():
    """Only one growth name out of five (20%) is below the majority
    threshold — must not tip the classification."""
    mixed = [
        {"ticker": "MSFT", "name": "Microsoft"},
        {"ticker": "XXXX", "name": "Unknown A"},
        {"ticker": "YYYY", "name": "Unknown B"},
        {"ticker": "ZZZZ", "name": "Unknown C"},
        {"ticker": "WWWW", "name": "Unknown D"},
    ]
    result = classify_style("etf", holdings=mixed, index_name=None)
    assert result["style"] is None
