"""Fundamentals sanity (plan 2026-10-12, D8 / WP6)."""
from __future__ import annotations

from unittest.mock import patch

from conftest import _memory_db

from app.decision.discover import pipeline
from app.decision.discover.dossier_writer import _fetch_fundamentals
from app.decision.discover.pipeline import _apply_analyst_consensus, stage_sentiment_fundamentals


def _fund(data, source="finnhub"):
    return {"ticker": "X", "data": data, "source": source, "stale": False}


def _stage(data, source="finnhub"):
    db = _memory_db()
    with patch("app.foundation.market.fundamentals", return_value=_fund(data, source)):
        scores, _ = stage_sentiment_fundamentals(db, "JPM", is_etf=True)
    assert scores is not None
    return scores


def test_bank_revenue_growth_is_dropped_with_a_concern():
    scores = _stage({"revenue_growth": 1.09, "industry": "Banking", "roe": 0.17, "pe_ratio": 14.0})
    assert scores["revenue_growth"] is None
    assert "revenue_growth_unreliable" in scores["concerns"]


def test_financials_sector_is_dropped_even_when_moderate():
    scores = _stage({"revenue_growth": 0.12, "sector": "Financial Services"}, source="yfinance")
    assert scores["revenue_growth"] is None


def test_extreme_growth_needs_a_second_provider():
    assert _stage({"revenue_growth": 1.5, "industry": "Semiconductors"})["revenue_growth"] is None
    with patch.object(pipeline, "_yfinance_revenue_growth", return_value=1.45):
        assert _stage({"revenue_growth": 1.5, "industry": "Semiconductors"})["revenue_growth"] == 1.5


def test_ordinary_growth_is_kept():
    scores = _stage({"revenue_growth": 0.18, "industry": "Software"})
    assert scores["revenue_growth"] == 0.18
    assert "revenue_growth_unreliable" not in scores["concerns"]


def test_roe_above_100_percent_shows_not_meaningful():
    db = _memory_db()
    with patch("app.foundation.market.fundamentals", return_value=_fund({"roe": 6.31, "pe_ratio": 30.0})):
        facts = _fetch_fundamentals("AAPL", db)
    assert facts is not None
    assert facts["roe"] == "n/m"
    assert facts["pe_basis"] == "trailing"


def test_analyst_target_records_kind_source_and_date():
    result = {"analyst_estimate_score": None}
    _apply_analyst_consensus(result, {
        "provider": "yfinance",
        "as_of": "2026-10-12T08:00:00",
        "data": {"target_mean": 110.0, "target_median": 105.0, "current_price": 100.0, "number_analysts": 30},
    })
    assert result["analyst_target"] == 105.0
    assert result["analyst_target_kind"] == "median"
    assert result["analyst_as_of"] == "2026-10-12"
    assert result["analyst_source"] == "yfinance"
