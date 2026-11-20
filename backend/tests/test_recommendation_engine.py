"""Tests for the Investment Recommendation Engine.

Covers:
- Pydantic model creation and serialization
- Context builder with mocked services
- Validator source attribution checking
- Orchestrator LLM response parsing
- Prompts template generation
- v2_adapter (RecommendationItem -> RecommendationPayloadV2) scoring/verdict logic
- The recommendation_item_to_v2 facade export (app.decision.recommendation_engine)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.foundation.schemas import RecommendationPayloadV2
from app.decision.recommendation_engine.models import (
    ContextBundle,
    DataHealth,
    Evidence,
    PortfolioContext,
    RecommendationItem,
    RecommendationReport,
    RegimeContext,
    TickerFundamentals,
    TickerMetrics,
    TickerSentiment,
    TickerTrackRecord,
)
from app.decision.recommendation_engine.prompts import (
    build_recommendation_prompt,
    _build_sources_inventory,
)
from app.decision.recommendation_engine.v2_adapter import (
    _expected_role,
    _portfolio_fit_score,
    _risk_score,
    _verdict_from_conviction,
    recommendation_item_to_v2,
)
from app.decision.recommendation_engine.validator import (
    _build_valid_sources,
    _is_source_valid,
    validate_report,
)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Test Pydantic model creation and serialization."""

    def test_evidence_creation(self):
        e = Evidence(
            source="metrics.VWCE.DE.sortino",
            value=1.2,
            interpretation="Good risk-adjusted returns",
        )
        assert e.source == "metrics.VWCE.DE.sortino"
        assert e.value == 1.2

    def test_evidence_defaults(self):
        e = Evidence(source="test")
        assert e.value is None
        assert e.interpretation == ""

    def test_recommendation_item_defaults(self):
        item = RecommendationItem(ticker="VWCE.DE", action="HOLD")
        assert item.confidence == "medium"
        assert item.timeframe == "3-6 months"
        assert item.evidence == []
        assert item.risks == []
        assert item.data_quality == "complete"

    def test_recommendation_report_safety_flags(self):
        report = RecommendationReport()
        assert report.estimate is True
        assert report.not_tax_advice is True

    def test_recommendation_report_serialization(self):
        report = RecommendationReport(
            report_id="test-123",
            recommendations=[
                RecommendationItem(
                    ticker="VWCE.DE",
                    action="BUY",
                    evidence=[
                        Evidence(
                            source="metrics.VWCE.DE.sortino",
                            value=1.5,
                            interpretation="Strong risk-adjusted returns",
                        )
                    ],
                )
            ],
        )
        data = report.model_dump()
        assert data["estimate"] is True
        assert data["not_tax_advice"] is True
        assert len(data["recommendations"]) == 1
        assert data["recommendations"][0]["ticker"] == "VWCE.DE"

    def test_context_bundle_defaults(self):
        cb = ContextBundle()
        assert cb.portfolio.total_value == 0.0
        assert cb.metrics == {}
        assert cb.fundamentals == {}
        assert cb.sentiment == {}
        assert cb.regime.label == "unknown"
        assert cb.user_profile.risk_tolerance == "moderate"

    def test_data_health_completeness(self):
        dh = DataHealth(
            available=["a", "b", "c"],
            failed=["d"],
            degraded=["e"],
            completeness_score=0.6,
        )
        assert dh.completeness_score == 0.6
        assert len(dh.available) == 3

    def test_ticker_metrics_defaults(self):
        tm = TickerMetrics()
        assert tm.sortino is None
        assert tm.sharpe is None
        assert tm.beta is None


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------


class TestValidator:
    """Test source attribution validation."""

    def _make_context(self) -> ContextBundle:
        """Create a test context bundle with some data."""
        return ContextBundle(
            metrics={
                "VWCE.DE": TickerMetrics(sortino=1.2, sharpe=0.8, calmar=0.5),
            },
            fundamentals={
                "VWCE.DE": TickerFundamentals(piotroski_score=7),
            },
            sentiment={
                "VWCE.DE": TickerSentiment(finbert_score=0.3, news_count=15),
            },
            track_record={
                "VWCE.DE": TickerTrackRecord(
                    advisor_composite_score=0.62, llm_win_rate=0.75, llm_verdict_history=["hit", "miss"],
                ),
            },
            regime=RegimeContext(label="bull", confidence=0.85),
        )

    def test_valid_source_recognized(self):
        ctx = self._make_context()
        valid = _build_valid_sources(ctx)
        assert "metrics.VWCE.DE.sortino" in valid
        assert "fundamentals.VWCE.DE.piotroski_score" in valid
        assert "sentiment.VWCE.DE.finbert_score" in valid
        assert "track_record.VWCE.DE.advisor_composite_score" in valid
        assert "track_record.VWCE.DE.llm_win_rate" in valid
        assert "track_record.VWCE.DE.llm_verdict_history" in valid
        assert "regime.label" in valid

    def test_track_record_source_absent_when_no_data(self):
        ctx = ContextBundle(track_record={"AAPL": TickerTrackRecord()})
        valid = _build_valid_sources(ctx)
        assert not any(s.startswith("track_record.AAPL") for s in valid)

    def test_evidence_citing_track_record_stripped_when_no_data(self):
        ctx = ContextBundle(track_record={"AAPL": TickerTrackRecord()})
        report = RecommendationReport(
            recommendations=[
                RecommendationItem(
                    ticker="AAPL",
                    action="BUY",
                    evidence=[Evidence(source="track_record.AAPL.advisor_composite_score", value=0.9)],
                )
            ],
        )
        validated, stripped = validate_report(report, ctx)
        assert len(stripped) == 1
        assert validated.recommendations[0].evidence == []

    def test_invalid_source_detected(self):
        assert not _is_source_valid("metrics.VWCE.DE.nonexistent", {"metrics.VWCE.DE.sortino"})

    def test_prefix_match_works(self):
        sources = {"metrics.VWCE.DE.sortino"}
        assert _is_source_valid("metrics.VWCE.DE", sources)

    def test_validate_report_strips_bad_sources(self):
        ctx = self._make_context()
        report = RecommendationReport(
            recommendations=[
                RecommendationItem(
                    ticker="VWCE.DE",
                    action="BUY",
                    evidence=[
                        Evidence(source="metrics.VWCE.DE.sortino", value=1.2),
                        Evidence(source="FAKE_SOURCE.xyz", value=999),
                    ],
                )
            ],
        )

        validated, stripped = validate_report(report, ctx)
        assert len(stripped) == 1
        assert "FAKE_SOURCE.xyz" in stripped[0]
        assert len(validated.recommendations[0].evidence) == 1
        assert validated.recommendations[0].evidence[0].source == "metrics.VWCE.DE.sortino"

    def test_validate_report_normalizes_action(self):
        ctx = self._make_context()
        report = RecommendationReport(
            recommendations=[
                RecommendationItem(ticker="VWCE.DE", action="buy"),
            ],
        )

        validated, _ = validate_report(report, ctx)
        assert validated.recommendations[0].action == "BUY"

    def test_validate_report_all_evidence_stripped_downgrades_confidence(self):
        ctx = self._make_context()
        report = RecommendationReport(
            recommendations=[
                RecommendationItem(
                    ticker="VWCE.DE",
                    action="BUY",
                    confidence="high",
                    evidence=[
                        Evidence(source="totally_fake_source", value=42),
                    ],
                )
            ],
        )

        validated, stripped = validate_report(report, ctx)
        assert len(stripped) == 1
        assert validated.recommendations[0].confidence == "low"

    def test_validate_empty_report(self):
        ctx = self._make_context()
        report = RecommendationReport(recommendations=[])
        validated, stripped = validate_report(report, ctx)
        assert stripped == []
        assert validated.recommendations == []


# ---------------------------------------------------------------------------
# Prompt tests
# ---------------------------------------------------------------------------


class TestPrompts:
    """Test LLM prompt generation."""

    def _make_context(self) -> ContextBundle:
        return ContextBundle(
            portfolio=PortfolioContext(
                holdings=[
                    {"ticker": "VWCE.DE", "current_value": 10000, "name": "Vanguard FTSE"},
                    {"ticker": "EUNL.DE", "current_value": 5000, "name": "iShares MSCI World"},
                ],
                total_value=15000,
            ),
            metrics={
                "VWCE.DE": TickerMetrics(sortino=1.2, sharpe=0.8),
            },
            regime=RegimeContext(label="bull", confidence=0.85),
        )

    def test_prompt_contains_available_data(self):
        ctx = self._make_context()
        prompt = build_recommendation_prompt(ctx)
        assert "metrics.VWCE.DE.sortino" in prompt
        assert "metrics.VWCE.DE.sharpe" in prompt
        assert "regime.label" in prompt

    def test_prompt_contains_portfolio_holdings(self):
        ctx = self._make_context()
        prompt = build_recommendation_prompt(ctx)
        assert "VWCE.DE" in prompt
        assert "EUNL.DE" in prompt

    def test_prompt_contains_safety_rules(self):
        ctx = self._make_context()
        prompt = build_recommendation_prompt(ctx)
        assert "CRITICAL RULES" in prompt
        assert "source" in prompt.lower()

    def test_prompt_contains_track_record_when_present(self):
        ctx = self._make_context().model_copy(
            update={"track_record": {"VWCE.DE": TickerTrackRecord(advisor_composite_score=0.5, llm_win_rate=0.8)}}
        )
        prompt = build_recommendation_prompt(ctx)
        assert "track_record.VWCE.DE.advisor_composite_score" in prompt
        assert "track_record.VWCE.DE.llm_win_rate" in prompt

    def test_prompt_omits_track_record_section_when_absent(self):
        ctx = self._make_context()
        prompt = build_recommendation_prompt(ctx)
        assert "### Track Record" not in prompt

    def test_prompt_json_schema(self):
        ctx = self._make_context()
        prompt = build_recommendation_prompt(ctx)
        assert "BUY" in prompt
        assert "HOLD" in prompt
        assert "SELL" in prompt

    def test_sources_inventory_includes_data_health(self):
        ctx = self._make_context()
        inventory = _build_sources_inventory(ctx)
        assert "Available sources" in inventory
        assert "Data Health" in inventory


# ---------------------------------------------------------------------------
# Orchestrator parse tests
# ---------------------------------------------------------------------------


class TestOrchestratorParsing:
    """Test LLM response parsing (no actual LLM calls)."""

    def test_parse_valid_json_array(self):
        from app.decision.recommendation_engine.orchestrator import _parse_llm_response

        raw = json.dumps([
            {
                "ticker": "VWCE.DE",
                "action": "BUY",
                "confidence": "high",
                "timeframe": "6-12 months",
                "thesis": "Strong fundamentals",
                "evidence": [
                    {"source": "metrics.VWCE.DE.sortino", "value": 1.2, "interpretation": "Good"}
                ],
                "risks": ["Market downturn"],
            }
        ])

        items = _parse_llm_response(raw)
        assert len(items) == 1
        assert items[0].ticker == "VWCE.DE"
        assert items[0].action == "BUY"
        assert items[0].confidence == "high"
        assert len(items[0].evidence) == 1
        assert items[0].risks == ["Market downturn"]

    def test_parse_markdown_wrapped_json(self):
        from app.decision.recommendation_engine.orchestrator import _parse_llm_response

        raw = '```json\n[{"ticker": "TEST", "action": "HOLD"}]\n```'
        items = _parse_llm_response(raw)
        assert len(items) == 1
        assert items[0].ticker == "TEST"

    def test_parse_invalid_json_returns_empty(self):
        from app.decision.recommendation_engine.orchestrator import _parse_llm_response

        items = _parse_llm_response("This is not JSON at all")
        assert items == []

    def test_parse_single_object_wrapped_in_array(self):
        from app.decision.recommendation_engine.orchestrator import _parse_llm_response

        raw = '{"ticker": "ABC", "action": "SELL"}'
        items = _parse_llm_response(raw)
        assert len(items) == 1
        assert items[0].ticker == "ABC"
        assert items[0].action == "SELL"

    def test_parse_empty_array(self):
        from app.decision.recommendation_engine.orchestrator import _parse_llm_response

        items = _parse_llm_response("[]")
        assert items == []

    def test_parse_malformed_items_skipped(self):
        from app.decision.recommendation_engine.orchestrator import _parse_llm_response

        raw = json.dumps([
            {"ticker": "GOOD", "action": "BUY"},
            "not a dict",
            {"ticker": "ALSO_GOOD", "action": "HOLD"},
        ])
        items = _parse_llm_response(raw)
        assert len(items) == 2

    def test_parse_extracts_bull_bear_and_horizon_months(self):
        from app.decision.recommendation_engine.orchestrator import _parse_llm_response

        raw = json.dumps([
            {
                "ticker": "VWCE.DE",
                "action": "BUY",
                "bull_case": "Broad diversification supports steady growth.",
                "bear_case": "A global downturn would hit all holdings at once.",
                "horizon_months": 6,
            }
        ])
        items = _parse_llm_response(raw)
        assert items[0].bull_case == "Broad diversification supports steady growth."
        assert items[0].bear_case == "A global downturn would hit all holdings at once."
        assert items[0].horizon_months == 6

    def test_parse_invalid_horizon_months_defaults_to_none(self):
        from app.decision.recommendation_engine.orchestrator import _parse_llm_response

        raw = json.dumps([{"ticker": "VWCE.DE", "action": "HOLD", "horizon_months": "a lot"}])
        items = _parse_llm_response(raw)
        assert items[0].horizon_months is None


# ---------------------------------------------------------------------------
# Context builder tests
# ---------------------------------------------------------------------------


def _memory_db():
    """Create an in-memory SQLite database session for testing.
    
    Returns a sessionmaker configured for in-memory DB with all tables created.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.foundation.core.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class TestContextBuilder:
    """Test context bundle construction with mocked services."""

    def test_context_builder_news_tracking(self):
        """_track_news_service should add news.{ticker} to available/degraded/failed."""
        from app.foundation.models.entities import NewsItem
        from app.decision.recommendation_engine.context_builder import _track_news_service

        db = _memory_db()()

        db.add(NewsItem(
            title="Test news",
            summary="body",
            url="https://example.com",
            source="test",
            ticker="VWCE.DE",
            published_at=datetime.now(timezone.utc),
        ))
        db.commit()

        available, failed, degraded = [], [], []
        _track_news_service(db, ["VWCE.DE", "EUNL.DE"], available, failed, degraded)

        assert any("news" in s for s in available + degraded)

    def test_context_builder_research_tracking(self):
        """_track_research_service should add research.{ticker} to available/degraded/failed."""
        from datetime import timedelta
        from app.foundation.models.entities import StockResearchReport, User
        from app.decision.recommendation_engine.context_builder import _track_research_service

        db = _memory_db()()

        user = User(username="test", password_hash="hash")
        db.add(user)
        db.commit()
        db.refresh(user)

        db.add(StockResearchReport(
            ticker="TEST.DE",
            user_id=user.id,
            report_json="{}",
            executive_summary="Test report",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        ))
        db.commit()

        available, failed, degraded = [], [], []
        _track_research_service(db, ["TEST.DE"], available, failed, degraded)

        assert any("research" in s for s in available + degraded)

    def test_track_record_degrades_cleanly_with_no_history(self):
        """No AdvisorScorecard/LlmPortfolioDecision rows exist yet — every
        field stays None/empty, and the ticker is marked degraded, not failed."""
        from app.foundation.models.entities import User
        from app.decision.recommendation_engine.context_builder import _build_ticker_track_record

        db = _memory_db()()
        user = User(username="track-record-empty", password_hash="hash")
        db.add(user)
        db.commit()
        db.refresh(user)

        available, failed, degraded = [], [], []
        result = _build_ticker_track_record(db, user.id, ["VWCE.DE"], available, failed, degraded)

        assert result["VWCE.DE"].advisor_composite_score is None
        assert result["VWCE.DE"].llm_verdict_history == []
        assert result["VWCE.DE"].llm_win_rate is None
        assert "track_record.VWCE.DE" in degraded

    def test_track_record_surfaces_advisor_scorecard_and_llm_verdicts(self):
        """A persisted AdvisorScorecard + scored LlmPortfolioDecision rows
        surface through both facade reads, not just direct DB access."""
        import json
        from datetime import date, timedelta
        from uuid import uuid4

        from app.foundation.models.entities import AdvisorScorecard, LlmPortfolioDecision, PaperPortfolio, User
        from app.decision.recommendation_engine.context_builder import _build_ticker_track_record

        db = _memory_db()()
        user = User(username="track-record-full", password_hash="hash")
        db.add(user)
        db.commit()
        db.refresh(user)

        portfolio = PaperPortfolio(id=uuid4().hex, user_id=user.id, name="Advisor", mandate="advisor")
        db.add(portfolio)
        db.add(AdvisorScorecard(
            id=uuid4().hex, user_id=user.id, portfolio_id=portfolio.id,
            window_start=date.today() - timedelta(days=90), window_end=date.today(),
            n_predictions=10, n_resolved=8,
            sharpe=1.1, sortino=1.4, calmar=0.9, psr=0.7,
            rps_avg=0.15, mz_slope=0.95, mz_r2=0.6, max_drawdown=0.08,
        ))
        db.add(LlmPortfolioDecision(
            id=uuid4().hex, portfolio_id=portfolio.id, review_date=datetime.now(timezone.utc),
            mandate="A", decision_json=json.dumps({"ticker": "VWCE.DE", "action": "buy"}),
            status="completed", verdict="hit",
            scored_at=datetime.now(timezone.utc),
        ))
        db.add(LlmPortfolioDecision(
            id=uuid4().hex, portfolio_id=portfolio.id, review_date=datetime.now(timezone.utc),
            mandate="B", decision_json=json.dumps({"ticker": "VWCE.DE", "action": "buy"}),
            status="completed", verdict="miss",
            scored_at=datetime.now(timezone.utc),
        ))
        db.commit()

        available, failed, degraded = [], [], []
        result = _build_ticker_track_record(db, user.id, ["VWCE.DE"], available, failed, degraded)

        assert result["VWCE.DE"].advisor_composite_score is not None
        assert result["VWCE.DE"].advisor_n_resolved == 8
        assert sorted(result["VWCE.DE"].llm_verdict_history) == ["hit", "miss"]
        assert result["VWCE.DE"].llm_win_rate == 0.5
        assert "track_record.advisor" in available
        assert "track_record.VWCE.DE" in available

    def test_candidate_tickers_override_held_tickers(self):
        """When candidate_tickers is given, per-ticker sections are built for
        the candidates, not the (empty) held-tickers list."""
        from app.foundation.models.entities import User
        from app.decision.recommendation_engine.context_builder import build_context_bundle

        db = _memory_db()()
        user = User(username="candidate-user", password_hash="hash")
        db.add(user)
        db.commit()
        db.refresh(user)

        context = build_context_bundle(db, user.id, candidate_tickers=["AAPL", "MSFT", "AAPL"])

        assert context.portfolio.holdings == []
        touched = set(context.data_health.available + context.data_health.failed + context.data_health.degraded)
        assert any("AAPL" in s for s in touched)
        assert any("MSFT" in s for s in touched)


# ---------------------------------------------------------------------------
# Prompt weights tests
# ---------------------------------------------------------------------------


class TestPromptWeights:
    """Test that weights section appears in prompt when provided."""

    def test_prompt_includes_weights_when_provided(self):
        ctx = ContextBundle(
            portfolio=PortfolioContext(
                holdings=[{"ticker": "VWCE.DE", "current_value": 10000}],
                total_value=10000,
            ),
            regime=RegimeContext(label="bull", confidence=0.8),
        )
        weights = {"quant": 0.4, "sentiment": 0.2, "fundamentals": 0.4}
        prompt = build_recommendation_prompt(ctx, weights=weights)
        assert "quant" in prompt
        assert "40%" in prompt
        assert "WEIGHTS" in prompt.upper() or "weight" in prompt.lower()

    def test_prompt_no_weights_section_when_none(self):
        ctx = ContextBundle(
            portfolio=PortfolioContext(
                holdings=[{"ticker": "VWCE.DE", "current_value": 10000}],
                total_value=10000,
            ),
        )
        prompt = build_recommendation_prompt(ctx, weights=None)
        # Should not crash, and should not have a weights section
        assert "CRITICAL RULES" in prompt


# ---------------------------------------------------------------------------
# v2_adapter tests (recommendation_item_to_v2 and its private helpers)
# ---------------------------------------------------------------------------
# Moved here from test_research_system.py (2026-08-26 facade-gap fix) so a
# future session searching by package name finds this coverage.


def _context_with_metrics(ticker: str, **metrics_kwargs) -> ContextBundle:
    return ContextBundle(metrics={ticker: TickerMetrics(**metrics_kwargs)})


class TestV2AdapterIntegration:
    """End-to-end recommendation_item_to_v2 conversions."""

    def test_recommendation_v2_schema_and_human_review_default(self):
        item = RecommendationItem(
            ticker="IWDA.AS",
            action="BUY",
            confidence="medium",
            thesis="Broad diversified core exposure.",
            evidence=[Evidence(source="metrics.IWDA.AS.sharpe", value=0.9, interpretation="Positive risk-adjusted return")],
            risks=["Market-wide drawdown risk"],
            data_quality="complete",
        )
        context = _context_with_metrics("IWDA.AS", sharpe=0.9, max_drawdown=0.12)

        payload = recommendation_item_to_v2(
            item,
            candidate={"ticker": "IWDA.AS", "name": "iShares Core MSCI World UCITS ETF", "type": "etf", "currency": "EUR"},
            context=context,
            mode="long_term",
        )

        validated = RecommendationPayloadV2.model_validate(payload.model_dump())
        assert validated.approval_state == "needs_review"
        assert validated.expires_at > validated.generated_at

    def test_high_drawdown_lowers_risk_score_and_avoid_verdict_carries_risk_notes(self):
        item = RecommendationItem(
            ticker="RISK",
            action="AVOID",
            confidence="low",
            thesis="Elevated drawdown makes this unattractive.",
            risks=["Backtest max drawdown exceeds 30%"],
            data_quality="partial",
        )
        context = _context_with_metrics("RISK", sharpe=1.1, max_drawdown=0.31)

        payload = recommendation_item_to_v2(
            item,
            candidate={"ticker": "RISK", "name": "Risk Asset", "type": "stock", "currency": "EUR"},
            context=context,
            mode="long_term",
        )

        assert payload.verdict == "AVOID"
        assert payload.risk_score <= 50
        assert payload.why_not_buy


class TestVerdictFromConviction:
    """_verdict_from_conviction: action + conviction -> verdict bucket."""

    def test_sell_is_always_rejected_by_risk(self):
        assert _verdict_from_conviction("SELL", 0.9) == "REJECTED_BY_RISK"
        assert _verdict_from_conviction("SELL", 0.0) == "REJECTED_BY_RISK"

    def test_hold_is_always_hold_existing(self):
        assert _verdict_from_conviction("HOLD", 0.9) == "HOLD_EXISTING"
        assert _verdict_from_conviction("HOLD", 0.0) == "HOLD_EXISTING"

    @pytest.mark.parametrize(
        ("conviction", "expected"),
        [
            (1.0, "STRONG_CANDIDATE"),
            (0.6, "STRONG_CANDIDATE"),   # >= 0.6 boundary, inclusive
            (0.59, "CANDIDATE"),         # just below the 0.6 boundary
            (0.35, "CANDIDATE"),         # >= 0.35 boundary, inclusive
            (0.34, "WATCH"),             # just below the 0.35 boundary
            (0.15, "WATCH"),             # >= 0.15 boundary, inclusive
            (0.14, "AVOID"),             # just below the 0.15 boundary
            (0.0, "AVOID"),
        ],
    )
    def test_buy_conviction_buckets(self, conviction, expected):
        assert _verdict_from_conviction("BUY", conviction) == expected

    def test_avoid_action_is_always_avoid(self):
        assert _verdict_from_conviction("AVOID", 0.9) == "AVOID"


class TestPortfolioFitScore:
    """_portfolio_fit_score: concentration penalty by asset-class weight band."""

    def _context(self, same_class_value: float, total_value: float, asset_type: str) -> ContextBundle:
        return ContextBundle(
            portfolio=PortfolioContext(
                holdings=[{"asset_type": asset_type, "current_value": same_class_value}],
                total_value=total_value,
            )
        )

    def test_concentration_at_or_above_40pct_scores_35(self):
        ctx = self._context(same_class_value=4500, total_value=10000, asset_type="etf")
        assert _portfolio_fit_score("etf", ctx) == 35.0

    def test_concentration_between_20_and_40pct_scores_55(self):
        ctx = self._context(same_class_value=2500, total_value=10000, asset_type="etf")
        assert _portfolio_fit_score("etf", ctx) == 55.0

    def test_concentration_below_20pct_scores_75(self):
        ctx = self._context(same_class_value=500, total_value=10000, asset_type="etf")
        assert _portfolio_fit_score("etf", ctx) == 75.0

    def test_zero_total_value_short_circuits_to_60(self):
        ctx = self._context(same_class_value=0, total_value=0, asset_type="etf")
        assert _portfolio_fit_score("etf", ctx) == 60.0


class TestExpectedRole:
    """_expected_role: asset_type (+ size_pct fallback) -> role classification."""

    @pytest.mark.parametrize("asset_type", ["cash", "money_market"])
    def test_cash_like_types(self, asset_type):
        assert _expected_role(asset_type, size_pct=5.0) == "cash_like"

    @pytest.mark.parametrize("asset_type", ["bond", "bond_etf"])
    def test_income_types(self, asset_type):
        assert _expected_role(asset_type, size_pct=5.0) == "income"

    @pytest.mark.parametrize("asset_type", ["etf", "fund"])
    def test_core_types(self, asset_type):
        assert _expected_role(asset_type, size_pct=5.0) == "core"

    def test_default_type_satellite_when_size_at_or_above_3pct(self):
        assert _expected_role("stock", size_pct=3.0) == "satellite"

    def test_default_type_speculative_when_size_below_3pct(self):
        assert _expected_role("stock", size_pct=2.9) == "speculative"


class TestRiskScore:
    """_risk_score: |max_drawdown| bucketed into a 0-100 safety score."""

    def test_none_drawdown_defaults_to_40(self):
        assert _risk_score(None) == 40.0

    def test_low_drawdown_scores_70(self):
        assert _risk_score(0.1) == 70.0

    def test_moderate_drawdown_scores_50(self):
        assert _risk_score(0.25) == 50.0

    def test_high_drawdown_scores_30(self):
        assert _risk_score(0.45) == 30.0


def test_recommendation_item_to_v2_is_exported_from_facade():
    """Guards the 2026-08-26 facade fix: recommendation_engine's package root
    must export recommendation_item_to_v2, not just v2_adapter internally."""
    from app.decision.recommendation_engine import recommendation_item_to_v2 as facade_fn

    assert facade_fn is recommendation_item_to_v2
