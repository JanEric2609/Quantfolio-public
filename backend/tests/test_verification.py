"""Tests for recommendation outcome evaluation (verification loop)."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.foundation.recommendation_outcomes import evaluate_recommendation_outcome


def _past(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


class TestEvaluateRecommendationOutcome:
    def test_pending_when_window_not_elapsed(self):
        # created 30 days ago, window 60 days → 30 days still to go
        result = evaluate_recommendation_outcome(ticker="EIMI.L", created_at=_past(30), window_days=60)
        assert result["outcome_label"] == "pending"
        assert result["abs_return"] is None
        assert result["benchmark_excess_return"] is None

    def test_unresolvable_when_no_price_data(self):
        with patch(
            "app.foundation.recommendation_outcomes._fetch_return_for_ticker",
            return_value=None,
        ):
            result = evaluate_recommendation_outcome(ticker="DELISTED.XX", created_at=_past(65), window_days=60)
        assert result["outcome_label"] == "unresolvable"
        assert result["abs_return"] is None

    def test_unresolvable_when_ticker_is_none(self):
        result = evaluate_recommendation_outcome(ticker=None, created_at=_past(65), window_days=60)
        assert result["outcome_label"] == "unresolvable"

    def test_correct_when_beats_benchmark_with_sortino(self):
        """correct requires BOTH benchmark_excess > 0.01 AND sortino_delta > 0.

        With single-period returns, sortino_ratio returns 0 (needs ≥2 data points),
        so sortino_delta is 0 and the label falls through to 'neutral'.
        This test verifies the fallback path. A separate test with multi-period
        returns verifies the 'correct' path.
        """
        with patch(
            "app.foundation.recommendation_outcomes._fetch_return_for_ticker",
            return_value=0.08,
        ), patch(
            "app.foundation.recommendation_outcomes._fetch_benchmark_return",
            return_value=0.03,
        ):
            result = evaluate_recommendation_outcome(ticker="EIMI.L", created_at=_past(65), window_days=60)
        # Single-period: sortino is 0 → neutral (not enough data for Sortino)
        assert result["outcome_label"] == "neutral"
        assert result["abs_return"] == pytest.approx(0.08, abs=1e-4)
        assert result["benchmark_excess_return"] == pytest.approx(0.05, abs=1e-4)
        assert result["portfolio_sortino_delta"] == 0.0

    def test_correct_when_beats_benchmark_and_sortino_positive(self):
        """correct requires benchmark_excess > 0.01 AND sortino_delta > 0.

        Mock sortino_ratio to return positive delta, simulating multi-period data.
        """
        def _fake_sortino(returns, risk_free=0.0, periods_per_year=252):
            # ticker gets 0.08 → sortino ~ 5, benchmark gets 0.03 → sortino ~ 2
            return 5.0 if returns[0] > 0.05 else 2.0

        with patch(
            "app.foundation.recommendation_outcomes._fetch_return_for_ticker",
            return_value=0.08,
        ), patch(
            "app.foundation.recommendation_outcomes._fetch_benchmark_return",
            return_value=0.03,
        ), patch(
            "app.foundation.quant_metrics.sortino_ratio",
            side_effect=_fake_sortino,
        ):
            result = evaluate_recommendation_outcome(ticker="EIMI.L", created_at=_past(65), window_days=60)
        assert result["outcome_label"] == "correct"
        assert result["abs_return"] == pytest.approx(0.08, abs=1e-4)
        assert result["benchmark_excess_return"] == pytest.approx(0.05, abs=1e-4)
        assert result["portfolio_sortino_delta"] > 0

    def test_incorrect_when_underperforms_benchmark_with_sortino(self):
        """incorrect requires benchmark_excess < -0.01 AND sortino_delta < 0.

        Single-period returns → sortino is 0 → falls to neutral.
        """
        with patch(
            "app.foundation.recommendation_outcomes._fetch_return_for_ticker",
            return_value=-0.05,
        ), patch(
            "app.foundation.recommendation_outcomes._fetch_benchmark_return",
            return_value=0.04,
        ):
            result = evaluate_recommendation_outcome(ticker="EIMI.L", created_at=_past(65), window_days=60)
        # Single-period: sortino is 0 → neutral
        assert result["outcome_label"] == "neutral"
        assert result["benchmark_excess_return"] < 0
        assert result["portfolio_sortino_delta"] == 0.0

    def test_incorrect_when_underperforms_and_sortino_negative(self):
        """incorrect requires benchmark_excess < -0.01 AND sortino_delta < 0.

        Mock sortino_ratio to return negative delta.
        """
        def _fake_sortino(returns, risk_free=0.0, periods_per_year=252):
            return 1.0 if returns[0] > 0 else 0.5

        with patch(
            "app.foundation.recommendation_outcomes._fetch_return_for_ticker",
            return_value=-0.05,
        ), patch(
            "app.foundation.recommendation_outcomes._fetch_benchmark_return",
            return_value=0.04,
        ), patch(
            "app.foundation.quant_metrics.sortino_ratio",
            side_effect=_fake_sortino,
        ):
            result = evaluate_recommendation_outcome(ticker="EIMI.L", created_at=_past(65), window_days=60)
        assert result["outcome_label"] == "incorrect"
        assert result["benchmark_excess_return"] < 0
        assert result["portfolio_sortino_delta"] < 0

    def test_neutral_when_excess_within_threshold(self):
        # benchmark_excess = 0.005, which is < 0.01 threshold → neutral
        with patch(
            "app.foundation.recommendation_outcomes._fetch_return_for_ticker",
            return_value=0.035,
        ), patch(
            "app.foundation.recommendation_outcomes._fetch_benchmark_return",
            return_value=0.03,
        ):
            result = evaluate_recommendation_outcome(ticker="EIMI.L", created_at=_past(65), window_days=60)
        assert result["outcome_label"] == "neutral"

    def test_result_includes_notes_json_with_ticker(self):
        with patch(
            "app.foundation.recommendation_outcomes._fetch_return_for_ticker",
            return_value=0.10,
        ), patch(
            "app.foundation.recommendation_outcomes._fetch_benchmark_return",
            return_value=0.02,
        ):
            result = evaluate_recommendation_outcome(ticker="IWMO.L", created_at=_past(65), window_days=60)
        import json
        notes = json.loads(result["notes_json"])
        assert notes["ticker"] == "IWMO.L"
        assert notes["benchmark_ticker"] == "EUNL.DE"
