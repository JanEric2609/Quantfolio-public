import pytest

from app.foundation.schemas import RecommendationPayload
from app.decision.ai import (
    LlmClient,
    deterministic_recommendation,
    strip_unverifiable_evidence,
    validate_json_payload,
)


def test_recommendation_validation_accepts_buy_payload():
    payload = validate_json_payload(
        RecommendationPayload,
        {
            "ticker": "EUNL.DE",
            "verdict": "BUY",
            "confidence": 0.7,
            "thesis": "Broad diversified exposure.",
            "portfolio_fit": "Fits the portfolio core allocation.",
            "dkb_available": True,
        },
    )

    assert payload.verdict == "BUY"


def test_recommendation_validation_rejects_sell_without_reasoning():
    with pytest.raises(ValueError):
        validate_json_payload(
            RecommendationPayload,
            {
                "ticker": "XYZ",
                "verdict": "SELL",
                "confidence": 0.8,
                "thesis": "Risk increased.",
                "portfolio_fit": "No longer fits.",
            },
        )


def test_deterministic_recommendation_is_not_placeholder():
    payload = deterministic_recommendation(
        "EUNL.DE",
        context={"backtest": {"metrics": {"sharpe": 1.1, "total_return": 0.12, "max_drawdown": 0.08}}},
    )

    assert payload.confidence > 40
    assert "placeholder" not in payload.thesis.lower()
    assert payload.backtest_summary["sharpe_ratio"] == 1.1


def test_structured_chat_retries_once_with_repair_payload():
    class FakeLlm(LlmClient):
        def __init__(self):
            self.calls = 0

        def _chat_content(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"ticker":"EUNL.DE","verdict":"BUY"}'
            return (
                '{"ticker":"EUNL.DE","verdict":"BUY","confidence":71,'
                '"thesis":"Diversified core exposure.","portfolio_fit":"Fits the core allocation."}'
            )

    client = FakeLlm()
    payload = client.structured_chat_with_repair([], RecommendationPayload)

    assert client.calls == 2
    assert payload.verdict == "BUY"


def test_strip_unverifiable_evidence_keeps_resolvable_paths():
    context = {
        "portfolio": {"holdings": [{"ticker": "EUNL.DE", "quantity": 10.0}]},
        "backtest": {"metrics": {"sharpe": 1.1}},
        "fundamentals": {"data": {"pe_ratio": 22.5}},
        "macro": [{"name": "inflation", "value": 2.1}],
    }
    evidence = [
        {"source": "backtest.metrics.sharpe", "value": 1.1, "interpretation": "Positive risk-adjusted return"},
        {"source": "fundamentals.data.pe_ratio", "value": 22.5, "interpretation": "Fair valuation"},
        {"source": "portfolio.holdings.0.ticker", "value": "EUNL.DE", "interpretation": "Already held"},
        {"source": "macro.0.value", "value": 2.1, "interpretation": "Inflation stable"},
    ]

    verified = strip_unverifiable_evidence(evidence, context)

    assert len(verified) == 4


def test_strip_unverifiable_evidence_drops_bogus_or_out_of_range_paths():
    context = {
        "backtest": {"metrics": {"sharpe": 1.1}},
        "portfolio": {"holdings": []},
    }
    evidence = [
        {"source": "backtest.metrics.sharpe", "value": 1.1, "interpretation": "Real"},
        {"source": "backtest.metrics.made_up_field", "value": 99, "interpretation": "Fabricated"},
        {"source": "portfolio.holdings.0.ticker", "value": "XYZ", "interpretation": "Empty list, out of range"},
        {"source": "not.a.real.path", "value": "?", "interpretation": "Nonsense"},
        {"value": 1, "interpretation": "Missing source key"},
    ]

    verified = strip_unverifiable_evidence(evidence, context)

    assert len(verified) == 1
    assert verified[0]["source"] == "backtest.metrics.sharpe"
