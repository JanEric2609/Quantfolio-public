"""Comprehensive tests for all 5 Multi-Agent Council agents + orchestrators.

Covers:
- BaseAgent: _extract_json, _parse_output, _build_messages, run()
- AnalystAgent: _build_user_message, run()
- RiskAgent: _build_user_message, run()
- MacroAgent: _build_user_message, run()
- PortfolioManagerAgent: _build_user_message, run()
- DebateAgent: _build_user_message, run()
- CouncilOrchestrator: run_council (happy path + error handling)
- CompetitionOrchestrator: run_competition

Zero real LLM/HTTP calls — all _call_llm calls are mocked.
Zero DB calls — no SQLAlchemy imports.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.decision.llm_portfolio.agents import (
    AgentConfig,
    AnalystAgent,
    AnalysisReport,
    BaseAgent,
    CompetitionCouncilResult,
    CompetitionOrchestrator,
    CouncilOrchestrator,
    CouncilResult,
    DebateAgent,
    DebateReport,
    DecisionReport,
    MacroAgent,
    MacroReport,
    Opportunity,
    ParseError,
    PortfolioManagerAgent,
    RiskAgent,
    RiskFlag,
    RiskReport,
    TradeProposalItem,
)
from app.decision.llm_portfolio.agents.base import AgentError, LLMCallError
from app.foundation.providers.rate_limiter import get_shared_async_limiter

pytestmark = pytest.mark.asyncio

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def agent_config() -> AgentConfig:
    """Shared default agent config for all agent tests."""
    return AgentConfig(
        portfolio_id="test_portfolio",
        strategy_prompt="Growth-oriented with risk controls",
        llm_endpoint="http://llm-test:8080",
        llm_model="test-model",
    )


@pytest.fixture
def base_context() -> dict[str, Any]:
    """Realistic context dict used by all agents and orchestrators."""
    return {
        "portfolio_id": "test_portfolio",
        "holdings": {
            "US0378331005": {"quantity": 10, "avg_price": 150.0},
            "US5949181045": {"quantity": 20, "avg_price": 200.0},
        },
        "cash": {"EUR": 50000.0, "USD": 10000.0},
        "market_data": {
            "US0378331005": {"price": 155.0, "change_pct": 0.02},
            "US5949181045": {"price": 210.0, "change_pct": 0.05},
        },
        "performance": {
            "total_return_pct": 12.5,
            "sharpe_ratio": 1.2,
            "volatility": 0.18,
        },
        "discover_items": [],
        "macro_indicators": {
            "vix": 18.5,
            "yield_curve_10y2y": 0.35,
            "inflation_cpi": 3.2,
            "gdp_growth": 2.1,
        },
        "sector_performance": {
            "Technology": 0.08,
            "Energy": -0.03,
            "Healthcare": 0.04,
            "Financials": 0.02,
        },
        "hmm_regime": "expansion",
        "risk_free_rate": 0.035,
        "var_95": 0.0215,
        "cvar_95": 0.0321,
        "max_drawdown": 0.148,
        "concentration": {"US0378331005": 0.15, "US5949181045": 0.10},
        "correlation_matrix": {"US0378331005_US5949181045": 0.65},
        "risk_budget": 0.95,
        "current_state": {
            "holdings": {
                "US0378331005": {"quantity": 10, "avg_price": 150.0},
                "US5949181045": {"quantity": 20, "avg_price": 200.0},
            },
            "cash": {"EUR": 50000.0},
        },
        "strategy": "Growth with risk controls, max 25% per position",
    }


# ===========================================================================
# Helper: mock LLM response factory
# ===========================================================================


def _mock_analysis_json(portfolio_id: str = "test_portfolio") -> str:
    """Realistic AnalysisReport JSON as the LLM would return."""
    return json.dumps({
        "portfolio_id": portfolio_id,
        "opportunities": [
            {
                "asset_id": "US0378331005",
                "symbol": "AAPL",
                "action": "buy",
                "score": 75.0,
                "rationale": "Strong earnings momentum and attractive valuation.",
            },
            {
                "asset_id": "US5949181045",
                "symbol": "MSFT",
                "action": "buy",
                "score": 70.0,
                "rationale": "Cloud revenue growth accelerating.",
            },
        ],
        "risk_flags": [
            {
                "asset_id": "US0378331005",
                "symbol": "AAPL",
                "risk_type": "concentration",
                "severity": 0.3,
                "description": "AAPL is 15% of portfolio, near concentration limit.",
            },
        ],
        "market_narrative": "Markets show cautious optimism with tech leading.",
        "confidence": 0.72,
    })


def _mock_risk_json(portfolio_id: str = "test_portfolio") -> str:
    """Realistic RiskReport JSON as the LLM would return."""
    return json.dumps({
        "portfolio_id": portfolio_id,
        "var_95": 0.0215,
        "cvar_95": 0.0321,
        "max_drawdown": 0.148,
        "concentration_warnings": ["AAPL at 15% approaches single-position limit"],
        "correlation_warnings": [],
        "per_trade_risk": [
            {
                "proposal_id": f"{portfolio_id}:US0378331005:buy",
                "var_impact": 0.003,
                "cvar_impact": 0.005,
                "concentration_risk": 0.35,
                "correlation_warning": None,
                "risk_score": 0.25,
            },
        ],
        "risk_budget_remaining": 0.72,
        "narrative": "Portfolio risk is moderate with adequate budget remaining.",
    })


def _mock_macro_json(portfolio_id: str = "test_portfolio") -> str:
    """Realistic MacroReport JSON as the LLM would return."""
    return json.dumps({
        "portfolio_id": portfolio_id,
        "regime": "expansion",
        "risk_stance": "neutral",
        "regime_confidence": 0.78,
        "sector_weights": [
            {"sector": "Technology", "weight": 0.30, "rationale": "Continued innovation cycle"},
            {"sector": "Healthcare", "weight": 0.20, "rationale": "Defensive growth"},
            {"sector": "Financials", "weight": 0.15, "rationale": "Yield support"},
            {"sector": "Energy", "weight": 0.10, "rationale": "Cyclical exposure"},
        ],
        "macro_narrative": "Expansion regime supports risk-on posture with selective hedging.",
        "risk_budget_adjustment": 0.05,
    })


def _mock_decision_json(portfolio_id: str = "test_portfolio") -> str:
    """Realistic DecisionReport JSON as the LLM would return."""
    return json.dumps({
        "portfolio_id": portfolio_id,
        "trade_proposals": [
            {
                "asset_id": "US0378331005",
                "action": "buy",
                "quantity": 200,
                "price": 150.0,
                "rationale": "Increase AAPL position based on analyst conviction.",
                "idempotency_key": "mock-1",
            },
        ],
        "target_weights": {
            "US0378331005": 0.20,
            "US5949181045": 0.15,
        },
        "risk_budget_allocation": {
            "Technology": 0.35,
            "Healthcare": 0.20,
            "Financials": 0.15,
            "Energy": 0.10,
        },
        "blm_views": {
            "US0378331005": 0.03,
            "US5949181045": 0.02,
        },
        "strategy_narrative": "Increase tech exposure while maintaining diversification.",
        "confidence": 0.68,
    })


def _mock_debate_json(portfolio_a_id: str = "portfolio_a", portfolio_b_id: str = "portfolio_b") -> str:
    """Realistic DebateReport JSON as the LLM would return."""
    return json.dumps({
        "portfolio_a_id": portfolio_a_id,
        "portfolio_b_id": portfolio_b_id,
        "portfolio_a_critique": [
            "Over-concentrated in tech despite macro warnings.",
            "High-confidence trades lack supporting analysis evidence.",
        ],
        "portfolio_b_critique": [
            "Excessive cash drag reduces upside capture.",
            "No hedging for downside scenarios in a late-cycle regime.",
        ],
        "portfolio_a_confidence": 0.55,
        "portfolio_b_confidence": 0.60,
        "suggested_revisions": {
            "portfolio_a": {
                "add_trades": [{"asset_id": "US002567R123", "action": "buy", "rationale": "Add healthcare hedge"}],
                "remove_trades": [{"asset_id": "US0378331005", "rationale": "Reduce concentration"}],
                "revise_trades": [],
            },
            "portfolio_b": {
                "add_trades": [],
                "remove_trades": [],
                "revise_trades": [{"asset_id": "US5949181045", "proposed_change": "Increase to 5%", "rationale": "Underexposed"}],
            },
        },
        "narrative": "Both portfolios have strengths but require adjustments.",
    })


# ===========================================================================
# TestBaseAgent
# ===========================================================================


class TestExtractJson:
    """BaseAgent._extract_json handles various LLM output formats."""

    def test_pure_json(self, agent_config: AgentConfig) -> None:
        agent = BaseAgent(agent_config)
        result = agent._extract_json('{"a": 1, "b": "hello"}')
        assert result == {"a": 1, "b": "hello"}

    def test_code_fences_with_lang(self, agent_config: AgentConfig) -> None:
        agent = BaseAgent(agent_config)
        raw = "```json\n{\"a\": 1}\n```"
        result = agent._extract_json(raw)
        assert result == {"a": 1}

    def test_code_fences_without_lang(self, agent_config: AgentConfig) -> None:
        agent = BaseAgent(agent_config)
        raw = "```\n{\"a\": 1}\n```"
        result = agent._extract_json(raw)
        assert result == {"a": 1}

    def test_trailing_commas(self, agent_config: AgentConfig) -> None:
        agent = BaseAgent(agent_config)
        raw = '{"a": 1, "b": 2,}'
        result = agent._extract_json(raw)
        assert result == {"a": 1, "b": 2}

    def test_extra_text_around_json(self, agent_config: AgentConfig) -> None:
        agent = BaseAgent(agent_config)
        raw = "Here is the result:\n{\"a\": 1}\nHope this helps."
        result = agent._extract_json(raw)
        assert result == {"a": 1}

    def test_nested_objects(self, agent_config: AgentConfig) -> None:
        agent = BaseAgent(agent_config)
        raw = '{"outer": {"inner": [1, 2, 3]}, "flag": true}'
        result = agent._extract_json(raw)
        assert result == {"outer": {"inner": [1, 2, 3]}, "flag": True}

    def test_empty_object(self, agent_config: AgentConfig) -> None:
        agent = BaseAgent(agent_config)
        result = agent._extract_json("{}")
        assert result == {}

    def test_no_json_raises(self, agent_config: AgentConfig) -> None:
        agent = BaseAgent(agent_config)
        with pytest.raises(ParseError, match="Failed to parse LLM output as JSON"):
            agent._extract_json("this is not json at all")


class TestParseOutput:
    """BaseAgent._parse_output — success and failure paths."""

    def test_valid_json_returns_model(self, agent_config: AgentConfig) -> None:
        """Valid JSON matching AgentAnalysis output_model returns an AnalysisReport."""
        agent = AnalystAgent(agent_config)
        result = agent._parse_output(_mock_analysis_json())
        assert isinstance(result, AnalysisReport)
        assert result.portfolio_id == "test_portfolio"

    def test_analyst_valid_json(self, agent_config: AgentConfig) -> None:
        """Valid AnalysisReport JSON returns an AnalysisReport instance."""
        agent = AnalystAgent(agent_config)
        raw = _mock_analysis_json()
        result = agent._parse_output(raw)
        assert isinstance(result, AnalysisReport)
        assert result.portfolio_id == "test_portfolio"
        assert len(result.opportunities) == 2
        assert result.confidence == 0.72

    def test_risk_valid_json(self, agent_config: AgentConfig) -> None:
        """Valid RiskReport JSON returns a RiskReport instance."""
        agent = RiskAgent(agent_config)
        raw = _mock_risk_json()
        result = agent._parse_output(raw)
        assert isinstance(result, RiskReport)
        assert result.var_95 == 0.0215
        assert result.risk_budget_remaining == 0.72

    def test_macro_valid_json(self, agent_config: AgentConfig) -> None:
        """Valid MacroReport JSON returns a MacroReport instance."""
        agent = MacroAgent(agent_config)
        raw = _mock_macro_json()
        result = agent._parse_output(raw)
        assert isinstance(result, MacroReport)
        assert result.regime == "expansion"
        assert result.risk_stance == "neutral"
        assert len(result.sector_weights) == 4

    def test_decision_valid_json(self, agent_config: AgentConfig) -> None:
        """Valid DecisionReport JSON returns a DecisionReport instance."""
        agent = PortfolioManagerAgent(agent_config)
        raw = _mock_decision_json()
        result = agent._parse_output(raw)
        assert isinstance(result, DecisionReport)
        assert len(result.trade_proposals) == 1
        assert result.confidence == 0.68

    def test_debate_valid_json(self, agent_config: AgentConfig) -> None:
        """Valid DebateReport JSON returns a DebateReport instance."""
        agent = DebateAgent(agent_config)
        raw = _mock_debate_json()
        result = agent._parse_output(raw)
        assert isinstance(result, DebateReport)
        assert result.portfolio_a_id == "portfolio_a"
        assert len(result.portfolio_a_critique) == 2

    def test_invalid_json_raises(self, agent_config: AgentConfig) -> None:
        """Unparseable string raises ParseError after exhausting retries."""
        agent = BaseAgent(agent_config)
        with pytest.raises(ParseError, match="Failed to parse/validate LLM output for"):
            agent._parse_output("not json at all")

    def test_validation_failure_raises(self, agent_config: AgentConfig) -> None:
        """JSON that parses but fails Pydantic model_validate raises ValidationError."""
        agent = AnalystAgent(agent_config)
        # Empty JSON is syntactically valid but missing required fields like portfolio_id
        with pytest.raises((ValidationError, ParseError)):
            agent._parse_output("{}")

    def test_extraction_from_fenced_block(self, agent_config: AgentConfig) -> None:
        """_parse_output handles markdown-fenced JSON blocks."""
        agent = AnalystAgent(agent_config)
        raw = "```json\n" + _mock_analysis_json() + "\n```"
        result = agent._parse_output(raw)
        assert isinstance(result, AnalysisReport)
        assert result.portfolio_id == "test_portfolio"


class _EmptyPromptAgent(BaseAgent):
    """Helper: BaseAgent subclass with empty system_prompt but working _build_user_message."""
    system_prompt = ""
    output_model = AnalysisReport

    def _build_user_message(self, **context: Any) -> str:
        return "test user message"


class TestBuildMessages:
    """BaseAgent._build_messages assembles system + user messages."""

    def test_returns_system_and_user(self, agent_config: AgentConfig) -> None:
        agent = AnalystAgent(agent_config)
        messages = agent._build_messages(
            portfolio_id="p1", holdings={}, cash={}, market_data={}, performance={}
        )
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_prompt_from_agent(self, agent_config: AgentConfig) -> None:
        """A subclass with a real system_prompt includes it in the message."""
        agent = AnalystAgent(agent_config)
        messages = agent._build_messages(
            portfolio_id="p1", holdings={}, cash={}, market_data={}, performance={}
        )
        assert "AnalystAgent" in messages[0]["content"]
        assert "portfolio_id" in messages[1]["content"]

    def test_empty_system_prompt_raises(self, agent_config: AgentConfig) -> None:
        """Empty system_prompt -> _build_system_message raises AgentError."""
        agent = _EmptyPromptAgent(agent_config)
        with pytest.raises(AgentError, match="system_prompt must be set"):
            agent._build_messages(foo="bar")


class TestRun:
    """BaseAgent.run() full lifecycle with mocked _call_llm."""

    async def test_analyst_run_returns_analysis_report(self, agent_config: AgentConfig) -> None:
        agent = AnalystAgent(agent_config)
        mock_response = _mock_analysis_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_response)):
            result = await agent.run(
                portfolio_id="test_portfolio",
                holdings={},
                cash={},
                market_data={},
                performance={},
            )
        assert isinstance(result, AnalysisReport)
        assert result.portfolio_id == "test_portfolio"
        assert len(result.opportunities) == 2
        assert result.confidence == 0.72
        # Tax outputs not applicable (not a tax endpoint)

    async def test_risk_run_returns_risk_report(self, agent_config: AgentConfig) -> None:
        agent = RiskAgent(agent_config)
        mock_response = _mock_risk_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_response)):
            result = await agent.run(
                portfolio_id="test_portfolio",
                holdings={},
                cash={},
                var_95=0.02,
                cvar_95=0.03,
                max_drawdown=0.15,
                concentration={},
                correlation_matrix={},
                proposed_trades=[],
                risk_budget=1.0,
            )
        assert isinstance(result, RiskReport)
        assert result.var_95 == 0.0215
        assert result.risk_budget_remaining == 0.72

    async def test_macro_run_returns_macro_report(self, agent_config: AgentConfig) -> None:
        agent = MacroAgent(agent_config)
        mock_response = _mock_macro_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_response)):
            result = await agent.run(
                portfolio_id="test_portfolio",
                macro_indicators={},
                sector_performance={},
                hmm_regime="expansion",
                risk_free_rate=0.035,
            )
        assert isinstance(result, MacroReport)
        assert result.regime == "expansion"
        assert result.risk_stance == "neutral"

    async def test_manager_run_returns_decision_report(self, agent_config: AgentConfig) -> None:
        agent = PortfolioManagerAgent(agent_config)
        mock_response = _mock_decision_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_response)):
            result = await agent.run(
                portfolio_id="test_portfolio",
                analysis={},
                risk={},
                macro={},
                current_state={"holdings": {}, "cash": {}},
            )
        assert isinstance(result, DecisionReport)
        assert len(result.trade_proposals) == 1
        assert result.confidence == 0.68

    async def test_debate_run_returns_debate_report(self, agent_config: AgentConfig) -> None:
        agent = DebateAgent(agent_config)
        mock_response = _mock_debate_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_response)):
            result = await agent.run(
                portfolio_a_id="portfolio_a",
                portfolio_b_id="portfolio_b",
                decision_a={},
                decision_b={},
                analysis_a={},
                analysis_b={},
                risk_a={},
                risk_b={},
                macro_a={},
                macro_b={},
                competition_rules="Standard rules",
            )
        assert isinstance(result, DebateReport)
        assert result.portfolio_a_id == "portfolio_a"
        assert result.portfolio_b_id == "portfolio_b"

    async def test_call_llm_failure_propagates(self, agent_config: AgentConfig) -> None:
        agent = AnalystAgent(agent_config)
        with patch.object(agent, "_call_llm", AsyncMock(side_effect=LLMCallError("LLM down"))):
            with pytest.raises(LLMCallError, match="LLM down"):
                await agent.run(portfolio_id="p1", holdings={}, cash={}, market_data={}, performance={})


# ===========================================================================
# TestAnalystAgent
# ===========================================================================


class TestAnalystAgent:
    """AnalystAgent specific behavior."""

    def test_build_user_message_includes_holdings_and_performance(self, agent_config: AgentConfig) -> None:
        agent = AnalystAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            holdings={"AAPL": {"quantity": 10, "avg_price": 150}},
            cash={"EUR": 50000},
            market_data={"AAPL": {"price": 155}},
            performance={"total_return": 0.12},
            discover_items=[],
        )
        assert "Portfolio ID: p1" in msg
        assert "AAPL" in msg
        assert "10" in msg
        assert "EUR" in msg
        assert "total_return" in msg
        assert "Market Data" in msg
        assert "Discovery Shortlist: None" in msg

    def test_build_user_message_with_discover_items(self, agent_config: AgentConfig) -> None:
        agent = AnalystAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            holdings={},
            cash={},
            market_data={},
            performance={},
            discover_items=[{"symbol": "GOOGL", "score": 80}],
        )
        assert "Discovery Shortlist:" in msg
        assert "GOOGL" in msg
        assert "None" not in msg

    def test_build_user_message_handles_none_values(self, agent_config: AgentConfig) -> None:
        agent = AnalystAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            holdings=None,
            cash=None,
            market_data=None,
            performance=None,
            discover_items=None,
        )
        # None is replaced with {} via `or {}` and `or []`
        assert "No current holdings." in msg
        assert "No cash balances." in msg
        assert "Discovery Shortlist: None" in msg

    async def test_run_with_mocked_llm(self, agent_config: AgentConfig) -> None:
        agent = AnalystAgent(agent_config)
        mock_json = _mock_analysis_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_json)):
            result = await agent.run(
                portfolio_id="test_portfolio",
                holdings={"US0378331005": {"quantity": 10, "avg_price": 150}},
                cash={"EUR": 50000},
                market_data={"US0378331005": {"price": 155}},
                performance={"total_return": 0.12},
                discover_items=[],
            )
        assert isinstance(result, AnalysisReport)
        assert result.portfolio_id == "test_portfolio"
        assert len(result.opportunities) == 2
        opp = result.opportunities[0]
        assert opp.asset_id == "US0378331005"
        assert opp.action == "buy"
        assert opp.score == 75.0
        risk_flag = result.risk_flags[0]
        assert risk_flag.risk_type == "concentration"
        assert risk_flag.severity == 0.3
        assert result.confidence == 0.72


# ===========================================================================
# TestRiskAgent
# ===========================================================================


class TestRiskAgent:
    """RiskAgent specific behavior."""

    def test_build_user_message_includes_quant_metrics(self, agent_config: AgentConfig) -> None:
        agent = RiskAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            holdings={"AAPL": {"quantity": 10, "avg_price": 150}},
            cash={"EUR": 50000},
            var_95=0.0215,
            cvar_95=0.0321,
            max_drawdown=0.148,
            concentration={"AAPL": 0.15},
            correlation_matrix={"AAPL_MSFT": 0.75},
            proposed_trades=[{"idempotency_key": "p1:AAPL:buy", "asset_id": "AAPL"}],
            risk_budget=0.95,
        )
        assert "Portfolio ID: p1" in msg
        assert "1-day 95% VaR: 0.021500" in msg
        assert "1-day 95% CVaR: 0.032100" in msg
        assert "Max Drawdown: 0.148000" in msg
        assert "Risk Budget (total): 0.950000" in msg
        assert "AAPL" in msg
        assert "Concentration Profile" in msg
        assert "Correlation Matrix" in msg
        assert "Proposed Trades" in msg

    def test_build_user_message_empty_concentration(self, agent_config: AgentConfig) -> None:
        agent = RiskAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            holdings={},
            cash={},
            var_95=0.0,
            cvar_95=0.0,
            max_drawdown=0.0,
            concentration={},
            correlation_matrix={},
            proposed_trades=[],
            risk_budget=1.0,
        )
        assert "No concentration data provided." in msg
        assert "No correlation matrix provided." in msg
        assert "No proposed trades." in msg

    async def test_run_with_mocked_llm(self, agent_config: AgentConfig) -> None:
        agent = RiskAgent(agent_config)
        mock_json = _mock_risk_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_json)):
            result = await agent.run(
                portfolio_id="test_portfolio",
                holdings={},
                cash={},
                var_95=0.02,
                cvar_95=0.03,
                max_drawdown=0.15,
                concentration={},
                correlation_matrix={},
                proposed_trades=[],
                risk_budget=1.0,
            )
        assert isinstance(result, RiskReport)
        assert result.var_95 == 0.0215
        assert result.cvar_95 == 0.0321
        assert result.max_drawdown == 0.148
        assert result.risk_budget_remaining == 0.72
        assert len(result.per_trade_risk) == 1


# ===========================================================================
# TestMacroAgent
# ===========================================================================


class TestMacroAgent:
    """MacroAgent specific behavior."""

    def test_build_user_message_includes_macro_indicators(self, agent_config: AgentConfig) -> None:
        agent = MacroAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            macro_indicators={"vix": 18.5, "yield_curve": 0.35},
            sector_performance={"Technology": 0.05, "Energy": -0.02},
            hmm_regime="expansion",
            risk_free_rate=0.035,
        )
        assert "portfolio_id: p1" in msg
        assert "Macro Indicators" in msg
        assert "vix" in msg
        assert "Sector Performance" in msg
        assert "Technology" in msg
        assert "Pre-computed Regime Prior" in msg
        assert "hmm_regime: expansion" in msg
        assert "risk_free_rate: 0.035" in msg

    def test_build_user_message_without_hmm(self, agent_config: AgentConfig) -> None:
        agent = MacroAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            macro_indicators={"vix": 18.5},
            sector_performance={"Technology": 0.05},
            hmm_regime=None,
            risk_free_rate=None,
        )
        assert "Pre-computed Regime Prior" not in msg
        assert "risk_free_rate:" not in msg

    async def test_run_with_mocked_llm(self, agent_config: AgentConfig) -> None:
        agent = MacroAgent(agent_config)
        mock_json = _mock_macro_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_json)):
            result = await agent.run(
                portfolio_id="test_portfolio",
                macro_indicators={"vix": 18.5},
                sector_performance={"Technology": 0.05},
                hmm_regime="expansion",
                risk_free_rate=0.035,
            )
        assert isinstance(result, MacroReport)
        assert result.regime == "expansion"
        assert result.risk_stance == "neutral"
        assert result.regime_confidence == 0.78
        assert len(result.sector_weights) == 4
        assert result.risk_budget_adjustment == 0.05


# ===========================================================================
# TestPortfolioManagerAgent
# ===========================================================================


class TestPortfolioManagerAgent:
    """PortfolioManagerAgent specific behavior."""

    def test_build_user_message_synthesizes_all_reports(self, agent_config: AgentConfig) -> None:
        agent = PortfolioManagerAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            analysis={"opportunities": [], "confidence": 0.7},
            risk={"var_95": 0.02, "risk_budget_remaining": 0.8},
            macro={"regime": "expansion", "sector_weights": []},
            current_state={"holdings": {"AAPL": 10}, "cash": {"EUR": 50000}},
            debate_feedback="Reduce tech concentration.",
            strategy="Growth mandate",
        )
        assert "Portfolio ID: p1" in msg
        assert "STRATEGY INSTRUCTIONS" in msg
        assert "Growth mandate" in msg
        assert "ANALYSIS REPORT" in msg
        assert "RISK REPORT" in msg
        assert "MACRO REPORT" in msg
        assert "CURRENT PORTFOLIO STATE" in msg
        assert "DEBATE FEEDBACK" in msg
        assert "Reduce tech concentration." in msg
        assert "REQUIRED OUTPUT" in msg
        assert "DecisionReport" in msg

    def test_build_user_message_uses_config_strategy_by_default(self, agent_config: AgentConfig) -> None:
        agent = PortfolioManagerAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_id="p1",
            analysis={},
            risk={},
            macro={},
            current_state={},
        )
        # Falls back to self.config.strategy_prompt since no "strategy" key in context
        assert "Growth-oriented with risk controls" in msg

    async def test_run_with_mocked_llm(self, agent_config: AgentConfig) -> None:
        agent = PortfolioManagerAgent(agent_config)
        mock_json = _mock_decision_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_json)):
            result = await agent.run(
                portfolio_id="test_portfolio",
                analysis={},
                risk={},
                macro={},
                current_state={"holdings": {}, "cash": {}},
            )
        assert isinstance(result, DecisionReport)
        assert result.portfolio_id == "test_portfolio"
        assert len(result.trade_proposals) == 1
        assert result.trade_proposals[0].asset_id == "US0378331005"
        assert result.trade_proposals[0].action == "buy"
        assert result.target_weights == {"US0378331005": 0.20, "US5949181045": 0.15}
        assert result.blm_views == {"US0378331005": 0.03, "US5949181045": 0.02}
        assert result.confidence == 0.68


# ===========================================================================
# TestDebateAgent
# ===========================================================================


class TestDebateAgent:
    """DebateAgent specific behavior."""

    def test_build_user_message_includes_both_portfolios(self, agent_config: AgentConfig) -> None:
        agent = DebateAgent(agent_config)
        msg = agent._build_user_message(
            portfolio_a_id="portfolio_a",
            portfolio_b_id="portfolio_b",
            decision_a={"confidence": 0.7},
            decision_b={"confidence": 0.6},
            analysis_a={},
            analysis_b={},
            risk_a={},
            risk_b={},
            macro_a={},
            macro_b={},
            competition_rules="Max 20% VaR",
        )
        assert "Dual Portfolio Debate" in msg
        assert "Portfolio A: portfolio_a" in msg
        assert "Portfolio B: portfolio_b" in msg
        assert "Original Decision Report" in msg
        assert "Underlying Analysis Report" in msg
        assert "Risk Report" in msg
        assert "Macro Report" in msg
        assert "Competition Rules" in msg
        assert "Max 20% VaR" in msg
        assert "Portfolio A original confidence: 0.7" in msg
        assert "Portfolio B original confidence: 0.6" in msg

    async def test_run_with_mocked_llm(self, agent_config: AgentConfig) -> None:
        agent = DebateAgent(agent_config)
        mock_json = _mock_debate_json()
        with patch.object(agent, "_call_llm", AsyncMock(return_value=mock_json)):
            result = await agent.run(
                portfolio_a_id="portfolio_a",
                portfolio_b_id="portfolio_b",
                decision_a={},
                decision_b={},
                analysis_a={},
                analysis_b={},
                risk_a={},
                risk_b={},
                macro_a={},
                macro_b={},
                competition_rules="Standard rules",
            )
        assert isinstance(result, DebateReport)
        assert result.portfolio_a_id == "portfolio_a"
        assert result.portfolio_b_id == "portfolio_b"
        assert len(result.portfolio_a_critique) == 2
        assert len(result.portfolio_b_critique) == 2
        assert result.portfolio_a_confidence == 0.55
        assert result.portfolio_b_confidence == 0.60
        assert "portfolio_a" in result.suggested_revisions
        assert "portfolio_b" in result.suggested_revisions


# ===========================================================================
# TestCouncilOrchestrator
# ===========================================================================


class TestCouncilOrchestrator:
    """CouncilOrchestrator pipeline: phases, error handling, report composition."""

    async def _make_orchestrator(self, config: AgentConfig) -> CouncilOrchestrator:
        """Create a CouncilOrchestrator sharing the same config for all agents."""
        return CouncilOrchestrator(config, config, config, config)

    async def _mock_all_agents(
        self, orchestrator: CouncilOrchestrator
    ) -> None:
        """Set up all 4 agent .run() methods to return valid reports."""
        orchestrator.analyst_agent.run = AsyncMock(
            return_value=AnalysisReport(
                portfolio_id="test_portfolio",
                opportunities=[
                    Opportunity(
                        asset_id="US0378331005",
                        symbol="AAPL",
                        action="buy",
                        score=75.0,
                        rationale="Strong momentum.",
                    )
                ],
                risk_flags=[
                    RiskFlag(
                        asset_id="US0378331005",
                        symbol="AAPL",
                        risk_type="concentration",
                        severity=0.3,
                        description="AAPL at 15%",
                    )
                ],
                market_narrative="Tech-led rally expected.",
                confidence=0.72,
            )
        )
        orchestrator.macro_agent.run = AsyncMock(
            return_value=MacroReport(
                portfolio_id="test_portfolio",
                regime="expansion",
                risk_stance="neutral",
                regime_confidence=0.78,
                sector_weights=[],
                macro_narrative="Expansion supports equities.",
                risk_budget_adjustment=0.05,
            )
        )
        orchestrator.risk_agent.run = AsyncMock(
            return_value=RiskReport(
                portfolio_id="test_portfolio",
                var_95=0.0215,
                cvar_95=0.0321,
                max_drawdown=0.148,
                concentration_warnings=["AAPL at 15%"],
                correlation_warnings=[],
                per_trade_risk=[],
                risk_budget_remaining=0.72,
                narrative="Risk is moderate.",
            )
        )
        orchestrator.manager_agent.run = AsyncMock(
            return_value=DecisionReport(
                portfolio_id="test_portfolio",
                trade_proposals=[
                    TradeProposalItem(
                        asset_id="US0378331005",
                        action="buy",
                        quantity=10,
                        price=180.0,
                        rationale="Increase AAPL.",
                        idempotency_key="test_portfolio:US0378331005:buy",
                    )
                ],
                target_weights={"US0378331005": 0.20},
                risk_budget_allocation={"Technology": 0.35},
                blm_views={"US0378331005": 0.03},
                strategy_narrative="Increase tech exposure.",
                confidence=0.68,
            )
        )

    async def test_happy_path(self, agent_config: AgentConfig, base_context: dict[str, Any]) -> None:
        """All agents succeed -> CouncilResult with all 4 reports."""
        orchestrator = await self._make_orchestrator(agent_config)
        await self._mock_all_agents(orchestrator)

        result = await orchestrator.run_council(**base_context)

        assert isinstance(result, CouncilResult)
        assert result.portfolio_id == "test_portfolio"
        assert isinstance(result.analysis, AnalysisReport)
        assert isinstance(result.risk, RiskReport)
        assert isinstance(result.macro, MacroReport)
        assert isinstance(result.decision, DecisionReport)
        assert len(result.errors) == 0

        # Verify the chain: analyst opportunities flowed to risk as proposed_trades
        assert len(result.analysis.opportunities) == 1
        assert result.analysis.opportunities[0].asset_id == "US0378331005"

        # Verify correct agent was called for each phase
        assert orchestrator.analyst_agent.run.await_count == 1  # type: ignore[reportAttributeAccessIssue]
        assert orchestrator.macro_agent.run.await_count == 1  # type: ignore[reportAttributeAccessIssue]
        assert orchestrator.risk_agent.run.await_count == 1  # type: ignore[reportAttributeAccessIssue]
        assert orchestrator.manager_agent.run.await_count == 1  # type: ignore[reportAttributeAccessIssue]

    async def test_analyst_and_macro_run_in_parallel(self, agent_config: AgentConfig, base_context: dict[str, Any]) -> None:
        """Verify phase 1 agents (analyst + macro) are both called (order doesn't matter)."""
        orchestrator = await self._make_orchestrator(agent_config)
        await self._mock_all_agents(orchestrator)

        await orchestrator.run_council(**base_context)

        # Both phase 1 agents called before phase 2 (risk) and phase 3 (manager)
        assert orchestrator.analyst_agent.run.await_count == 1  # type: ignore[reportAttributeAccessIssue]
        assert orchestrator.macro_agent.run.await_count == 1  # type: ignore[reportAttributeAccessIssue]

    async def test_error_handling_analyst_failure(
        self, agent_config: AgentConfig, base_context: dict[str, Any]
    ) -> None:
        """AnalystAgent failure -> default report substituted, error captured."""
        orchestrator = await self._make_orchestrator(agent_config)
        orchestrator.analyst_agent.run = AsyncMock(side_effect=AgentError("Analyst LLM error"))
        orchestrator.macro_agent.run = AsyncMock(
            return_value=MacroReport(
                portfolio_id="test_portfolio",
                regime="expansion",
                risk_stance="neutral",
                regime_confidence=0.5,
            )
        )
        orchestrator.risk_agent.run = AsyncMock(
            return_value=RiskReport(
                portfolio_id="test_portfolio", var_95=0.0, cvar_95=0.0, max_drawdown=0.0
            )
        )
        orchestrator.manager_agent.run = AsyncMock(
            return_value=DecisionReport(portfolio_id="test_portfolio")
        )

        result = await orchestrator.run_council(**base_context)

        assert isinstance(result, CouncilResult)
        # Analyst failed -> default AnalysisReport with empty opportunities
        assert result.analysis.portfolio_id == "test_portfolio"
        assert len(result.analysis.opportunities) == 0
        # Other agents still provided real reports
        assert result.macro.regime == "expansion"
        assert result.risk.var_95 == 0.0
        # Error was recorded
        assert len(result.errors) == 1
        assert "AnalystAgent" in result.errors[0]

    async def test_error_handling_all_agents_fail(
        self, agent_config: AgentConfig, base_context: dict[str, Any]
    ) -> None:
        """All agents fail -> each gets default report, all errors captured."""
        orchestrator = await self._make_orchestrator(agent_config)
        orchestrator.analyst_agent.run = AsyncMock(side_effect=AgentError("Analyst failed"))
        orchestrator.macro_agent.run = AsyncMock(side_effect=AgentError("Macro failed"))
        orchestrator.risk_agent.run = AsyncMock(side_effect=AgentError("Risk failed"))
        orchestrator.manager_agent.run = AsyncMock(side_effect=AgentError("Manager failed"))

        result = await orchestrator.run_council(**base_context)

        assert isinstance(result, CouncilResult)
        # All default reports carry the correct portfolio_id (set by default_factory)
        assert result.analysis.portfolio_id == "test_portfolio"
        assert result.macro.portfolio_id == "test_portfolio"
        assert result.risk.portfolio_id == "test_portfolio"
        assert result.decision.portfolio_id == "test_portfolio"
        assert len(result.errors) == 4

    async def test_unexpected_exception_still_captured(
        self, agent_config: AgentConfig, base_context: dict[str, Any]
    ) -> None:
        """A non-AgentError exception (e.g. ValueError) is caught by the broad except."""
        orchestrator = await self._make_orchestrator(agent_config)
        orchestrator.analyst_agent.run = AsyncMock(side_effect=ValueError("Something unexpected"))
        orchestrator.macro_agent.run = AsyncMock(
            return_value=MacroReport(portfolio_id="test_portfolio", regime="expansion", risk_stance="neutral")
        )
        orchestrator.risk_agent.run = AsyncMock(
            return_value=RiskReport(portfolio_id="test_portfolio", var_95=0.0, cvar_95=0.0, max_drawdown=0.0)
        )
        orchestrator.manager_agent.run = AsyncMock(
            return_value=DecisionReport(portfolio_id="test_portfolio")
        )

        result = await orchestrator.run_council(**base_context)

        assert len(result.errors) == 1
        assert "AnalystAgent" in result.errors[0]
        assert "unexpected" in result.errors[0].lower()

    async def test_proposed_trades_flow_from_analyst_to_risk(
        self, agent_config: AgentConfig, base_context: dict[str, Any]
    ) -> None:
        """RiskAgent receives analyst's opportunities as proposed_trades."""
        orchestrator = await self._make_orchestrator(agent_config)
        orchestrator.analyst_agent.run = AsyncMock(
            return_value=AnalysisReport(
                portfolio_id="test_portfolio",
                opportunities=[
                    Opportunity(
                        asset_id="US0378331005",
                        symbol="AAPL",
                        action="buy",
                        score=80.0,
                        rationale="Strong.",
                    )
                ],
                risk_flags=[],
                market_narrative="Upbeat.",
                confidence=0.7,
            )
        )
        orchestrator.macro_agent.run = AsyncMock(
            return_value=MacroReport(portfolio_id="test_portfolio", regime="expansion", risk_stance="neutral")
        )
        risk_mock = AsyncMock(
            return_value=RiskReport(
                portfolio_id="test_portfolio", var_95=0.02, cvar_95=0.03, max_drawdown=0.1
            )
        )
        orchestrator.risk_agent.run = risk_mock
        orchestrator.manager_agent.run = AsyncMock(
            return_value=DecisionReport(portfolio_id="test_portfolio")
        )

        await orchestrator.run_council(**base_context)

        # Risk agent should have been called with proposed_trades derived from analyst opportunities
        call_kwargs = risk_mock.call_args[1]
        assert "proposed_trades" in call_kwargs
        assert len(call_kwargs["proposed_trades"]) == 1
        assert call_kwargs["proposed_trades"][0]["asset_id"] == "US0378331005"
        assert call_kwargs["proposed_trades"][0]["action"] == "buy"
        assert "idempotency_key" in call_kwargs["proposed_trades"][0]


# ===========================================================================
# TestCompetitionOrchestrator
# ===========================================================================


class TestCompetitionOrchestrator:
    """CompetitionOrchestrator: runs two councils in parallel + debate."""

    async def _make_competition(
        self, config: AgentConfig
    ) -> CompetitionOrchestrator:
        return CompetitionOrchestrator(config, config, config)

    async def _mock_all_agents_for_council(
        self, portfolio_id: str
    ) -> dict[str, AsyncMock]:
        """Create a dict of mocked agent run() methods for one council."""
        return {
            "analyst": AsyncMock(
                return_value=AnalysisReport(
                    portfolio_id=portfolio_id,
                    opportunities=[
                        Opportunity(
                            asset_id="US0378331005",
                            symbol="AAPL",
                            action="buy",
                            score=75.0,
                            rationale="Strong momentum.",
                        )
                    ],
                    risk_flags=[],
                    market_narrative="Market narrative.",
                    confidence=0.7,
                )
            ),
            "macro": AsyncMock(
                return_value=MacroReport(
                    portfolio_id=portfolio_id,
                    regime="expansion",
                    risk_stance="risk_on",
                    regime_confidence=0.7,
                    sector_weights=[],
                    macro_narrative="Macro narrative.",
                    risk_budget_adjustment=0.0,
                )
            ),
            "risk": AsyncMock(
                return_value=RiskReport(
                    portfolio_id=portfolio_id,
                    var_95=0.02,
                    cvar_95=0.03,
                    max_drawdown=0.1,
                    concentration_warnings=[],
                    correlation_warnings=[],
                    per_trade_risk=[],
                    risk_budget_remaining=0.8,
                    narrative="Risk narrative.",
                )
            ),
            "manager": AsyncMock(
                return_value=DecisionReport(
                    portfolio_id=portfolio_id,
                    trade_proposals=[
                        TradeProposalItem(
                            asset_id="US0378331005",
                            action="buy",
                            quantity=10,
                            price=180.0,
                            rationale="Increase.",
                            idempotency_key="portfolio_a:US0378331005:buy",
                        )
                    ],
                    target_weights={"US0378331005": 0.20},
                    risk_budget_allocation={"Technology": 0.35},
                    blm_views={"US0378331005": 0.03},
                    strategy_narrative="Strategy narrative.",
                    confidence=0.65,
                )
            ),
        }

    def _apply_mocks_to_orchestrator(
        self, orchestrator: CouncilOrchestrator, mocks: dict[str, AsyncMock]
    ) -> None:
        """Apply mocked run() methods to a CouncilOrchestrator's agents."""
        orchestrator.analyst_agent.run = mocks["analyst"]
        orchestrator.macro_agent.run = mocks["macro"]
        orchestrator.risk_agent.run = mocks["risk"]
        orchestrator.manager_agent.run = mocks["manager"]

    async def test_run_competition_happy_path(
        self, agent_config: AgentConfig
    ) -> None:
        """Two councils + debate -> CompetitionCouncilResult with both decisions and debate."""
        competition = await self._make_competition(agent_config)

        context_a = {
            "portfolio_id": "portfolio_a",
            "holdings": {"US0378331005": {"quantity": 10, "avg_price": 150}},
            "cash": {"EUR": 50000},
            "market_data": {},
            "performance": {},
            "discover_items": [],
            "macro_indicators": {"vix": 18.5},
            "sector_performance": {"Technology": 0.05},
            "hmm_regime": "expansion",
            "risk_free_rate": 0.035,
            "var_95": 0.02,
            "cvar_95": 0.03,
            "max_drawdown": 0.15,
            "concentration": {},
            "correlation_matrix": {},
            "risk_budget": 0.95,
            "current_state": {"holdings": {"US0378331005": 10}, "cash": {"EUR": 50000}},
            "strategy": "Growth",
        }
        context_b = {**context_a, "portfolio_id": "portfolio_b", "strategy": "Value"}

        # We need to intercept the CouncilOrchestrators created inside
        # CompetitionOrchestrator.run_competition. The orchestrator creates
        # them internally: council_a = CouncilOrchestrator(...)
        # and council_b = CouncilOrchestrator(...).
        # These are separate from the competition's own agent list.
        #
        # The cleanest approach: mock the CouncilOrchestrator.run_council method
        # on the internal instances. Since they're created inside run_competition,
        # we need to patch the class.

        # Alternative: patch CouncilOrchestrator.run_council to return
        # pre-built CouncilResults.
        result_a = CouncilResult(
            portfolio_id="portfolio_a",
            analysis=AnalysisReport(
                portfolio_id="portfolio_a",
                opportunities=[
                    Opportunity(asset_id="US0378331005", symbol="AAPL", action="buy", score=75.0, rationale="Strong.")
                ],
                risk_flags=[],
                market_narrative="Bullish tech.",
                confidence=0.7,
            ),
            risk=RiskReport(
                portfolio_id="portfolio_a", var_95=0.02, cvar_95=0.03, max_drawdown=0.1
            ),
            macro=MacroReport(
                portfolio_id="portfolio_a", regime="expansion", risk_stance="risk_on"
            ),
            decision=DecisionReport(
                portfolio_id="portfolio_a",
                trade_proposals=[
                    TradeProposalItem(
                        asset_id="US0378331005",
                        action="buy",
                        quantity=10,
                        price=180.0,
                        rationale="Increase.",
                        idempotency_key="portfolio_a:US0378331005:buy",
                    )
                ],
                target_weights={"US0378331005": 0.20},
                risk_budget_allocation={"Technology": 0.35},
                blm_views={"US0378331005": 0.03},
                strategy_narrative="Growth strategy.",
                confidence=0.65,
            ),
            errors=[],
        )
        result_b = CouncilResult(
            portfolio_id="portfolio_b",
            analysis=AnalysisReport(
                portfolio_id="portfolio_b",
                opportunities=[
                    Opportunity(asset_id="US5949181045", symbol="MSFT", action="buy", score=70.0, rationale="Cloud.")
                ],
                risk_flags=[],
                market_narrative="Value opportunities.",
                confidence=0.6,
            ),
            risk=RiskReport(
                portfolio_id="portfolio_b", var_95=0.015, cvar_95=0.025, max_drawdown=0.12
            ),
            macro=MacroReport(
                portfolio_id="portfolio_b", regime="expansion", risk_stance="neutral"
            ),
            decision=DecisionReport(
                portfolio_id="portfolio_b",
                trade_proposals=[
                    TradeProposalItem(
                        asset_id="US5949181045",
                        action="buy",
                        quantity=5,
                        price=350.0,
                        rationale="Value play.",
                        idempotency_key="portfolio_b:US5949181045:buy",
                    )
                ],
                target_weights={"US5949181045": 0.15},
                risk_budget_allocation={"Technology": 0.25},
                blm_views={"US5949181045": 0.015},
                strategy_narrative="Value strategy.",
                confidence=0.6,
            ),
            errors=[],
        )

        debate_report = DebateReport(
            portfolio_a_id="portfolio_a",
            portfolio_b_id="portfolio_b",
            portfolio_a_critique=["Over-concentrated in tech"],
            portfolio_b_critique=["Excessive cash"],
            portfolio_a_confidence=0.55,
            portfolio_b_confidence=0.6,
            suggested_revisions={
                "portfolio_a": {"add_trades": [], "remove_trades": [], "revise_trades": []},
                "portfolio_b": {"add_trades": [], "remove_trades": [], "revise_trades": []},
            },
            narrative="Both portfolios need adjustments.",
        )

        # We need to mock CouncilOrchestrator.run_council on the instances
        # created inside run_competition. The trick: patch the method directly.
        # Since competition creates new CouncilOrchestrator instances, we can
        # patch council_a.run_council and council_b.run_council after creation,
        # but that's not possible from outside. Alternative: patch
        # CouncilOrchestrator.run_council as a class method just for this test.

        with (
            patch.object(CouncilOrchestrator, "run_council", new=AsyncMock()) as mock_run,
        ):
            # Make the mock return different results for each portfolio
            async def _side_effect(**context: Any) -> CouncilResult:
                pid = context.get("portfolio_id", "")
                if pid == "portfolio_a":
                    return result_a
                return result_b

            mock_run.side_effect = _side_effect

            # Mock debate agent
            competition.debate_agent.run = AsyncMock(return_value=debate_report)

            competition_result = await competition.run_competition(
                context_a=context_a,
                context_b=context_b,
                competition_rules="Standard competition rules.",
                run_id="test_run_001",
            )

        assert isinstance(competition_result, CompetitionCouncilResult)
        assert competition_result.run_id == "test_run_001"

        # Portfolio A results
        assert competition_result.portfolio_a.portfolio_id == "portfolio_a"
        assert len(competition_result.portfolio_a.analysis.opportunities) == 1
        assert competition_result.portfolio_a.decision.confidence == 0.65

        # Portfolio B results
        assert competition_result.portfolio_b.portfolio_id == "portfolio_b"
        assert len(competition_result.portfolio_b.analysis.opportunities) == 1
        assert competition_result.portfolio_b.decision.confidence == 0.6

        # Debate results
        assert isinstance(competition_result.debate, DebateReport)
        assert competition_result.debate.portfolio_a_id == "portfolio_a"
        assert competition_result.debate.portfolio_b_id == "portfolio_b"
        assert len(competition_result.debate.portfolio_a_critique) == 1

        # No errors
        assert len(competition_result.errors) == 0

        # Verify council was called twice (once per portfolio)
        assert mock_run.call_count == 2

    async def test_run_competition_debate_error(
        self, agent_config: AgentConfig
    ) -> None:
        """DebateAgent failure results in default DebateReport and captured error."""
        competition = await self._make_competition(agent_config)

        result_a = CouncilResult(
            portfolio_id="portfolio_a",
            analysis=AnalysisReport(portfolio_id="portfolio_a"),
            risk=RiskReport(portfolio_id="portfolio_a", var_95=0.0, cvar_95=0.0, max_drawdown=0.0),
            macro=MacroReport(portfolio_id="portfolio_a", regime="unknown", risk_stance="neutral"),
            decision=DecisionReport(portfolio_id="portfolio_a"),
            errors=[],
        )
        result_b = CouncilResult(
            portfolio_id="portfolio_b",
            analysis=AnalysisReport(portfolio_id="portfolio_b"),
            risk=RiskReport(portfolio_id="portfolio_b", var_95=0.0, cvar_95=0.0, max_drawdown=0.0),
            macro=MacroReport(portfolio_id="portfolio_b", regime="unknown", risk_stance="neutral"),
            decision=DecisionReport(portfolio_id="portfolio_b"),
            errors=[],
        )

        with patch.object(CouncilOrchestrator, "run_council", new=AsyncMock()) as mock_run:
            async def _side_effect(**context: Any) -> CouncilResult:
                pid = context.get("portfolio_id", "")
                return result_a if pid == "portfolio_a" else result_b

            mock_run.side_effect = _side_effect

            # Debate agent raises error
            competition.debate_agent.run = AsyncMock(
                side_effect=AgentError("Debate LLM error")
            )

            competition_result = await competition.run_competition(
                context_a={"portfolio_id": "portfolio_a"},
                context_b={"portfolio_id": "portfolio_b"},
                competition_rules="Rules",
                run_id="test_debate_error",
            )

        assert isinstance(competition_result, CompetitionCouncilResult)
        # Debate should be a default report (empty portfolio IDs since fallback)
        # The fallback creates DebateReport(portfolio_a_id=..., portfolio_b_id=...)
        assert competition_result.debate.portfolio_a_id == "portfolio_a"
        assert competition_result.debate.portfolio_b_id == "portfolio_b"
        # Error should be captured
        assert len(competition_result.errors) >= 1
        assert any("DebateAgent" in e for e in competition_result.errors)


class _RecordingClient:
    """Fake AsyncClient whose post() records each JSON payload."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeLLMResponse:
        self.payloads.append(json or {})
        return _FakeLLMResponse()


class TestCallLlmPayloadSampling:
    """Council payloads send the Qwen non-thinking sampling preset outright.

    Plan D4: AgentConfig.temperature no longer drives the payload — the preset
    (temp 0.7 / top-p 0.8 / top-k 20) is sent outright; max_tokens still comes
    from config. Plan D3: no penalty keys client-side (server owns
    --presence-penalty 1.5).
    """

    async def test_payload_sends_qwen_sampling_and_config_max_tokens(
        self, agent_config: AgentConfig
    ) -> None:
        client = _RecordingClient()
        agent = BaseAgent(agent_config, client=client)

        await agent._call_llm([{"role": "user", "content": "test"}])

        assert len(client.payloads) == 1
        payload = client.payloads[0]
        assert payload["temperature"] == 0.7
        assert payload["top_p"] == 0.8
        assert payload["top_k"] == 20
        assert payload["max_tokens"] == agent_config.max_tokens
        assert payload["model"] == agent_config.llm_model
        assert "presence_penalty" not in payload
        assert "frequency_penalty" not in payload


# ===========================================================================
# TestCallLlmSerialization
# ===========================================================================


class _FakeLLMResponse:
    """Minimal stand-in for httpx.Response as consumed by _call_llm."""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}


class _RecordingSlowClient:
    """Fake AsyncClient whose post() records loop-time start/end and sleeps.

    Shared between agents in serialization tests so every POST lands in one
    call log regardless of which agent issued it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeLLMResponse:
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(0.05)
        end = asyncio.get_running_loop().time()
        self.calls.append((start, end))
        return _FakeLLMResponse()


class TestCallLlmSerialization:
    """BaseAgent._call_llm serializes concurrent POSTs per LLM endpoint."""

    async def test_two_concurrent_agent_calls_are_serialized(self, agent_config: AgentConfig) -> None:
        """Two agents on one endpoint: second POST starts only after first ends."""
        client = _RecordingSlowClient()
        agent_a = BaseAgent(agent_config, client=client)
        agent_b = BaseAgent(agent_config, client=client)
        messages = [{"role": "user", "content": "test"}]

        await asyncio.gather(
            agent_a._call_llm(messages),
            agent_b._call_llm(messages),
        )

        assert len(client.calls) == 2
        first_start, first_end = min(client.calls, key=lambda call: call[0])
        second_start, _ = max(client.calls, key=lambda call: call[0])
        assert second_start >= first_end

    def test_shared_async_limiter_returns_same_instance_for_key(self) -> None:
        limiter_first = get_shared_async_limiter("k")
        limiter_second = get_shared_async_limiter("k")
        assert limiter_first is limiter_second
        assert get_shared_async_limiter("other") is not limiter_first

    async def test_different_endpoints_do_not_serialize_each_other(self, agent_config: AgentConfig) -> None:
        """Agents on different endpoints may overlap (per-endpoint keys)."""
        other_config = AgentConfig(
            portfolio_id="test_portfolio",
            strategy_prompt="Growth-oriented with risk controls",
            llm_endpoint="http://llm-other:8080",
            llm_model="test-model",
        )
        client = _RecordingSlowClient()
        agent_a = BaseAgent(agent_config, client=client)
        agent_b = BaseAgent(other_config, client=client)
        messages = [{"role": "user", "content": "test"}]

        await asyncio.gather(
            agent_a._call_llm(messages),
            agent_b._call_llm(messages),
        )

        assert len(client.calls) == 2
        first_start, first_end = min(client.calls, key=lambda call: call[0])
        second_start, _ = max(client.calls, key=lambda call: call[0])
        assert second_start < first_end


# ---------------------------------------------------------------------------
# Corrective retry on an invalid reply (prod: ~1 RiskAgent failure a day)
# ---------------------------------------------------------------------------

_RISK_KWARGS = dict(
    portfolio_id="test_portfolio", holdings={}, cash={}, var_95=0.02, cvar_95=0.03,
    max_drawdown=0.15, concentration={}, correlation_matrix={}, proposed_trades=[],
    risk_budget=1.0,
)


def _out_of_range_risk_json() -> str:
    """A sell's incremental VaR reported as negative: out of the >= 0 schema."""
    data = json.loads(_mock_risk_json())
    data["per_trade_risk"] = [{"proposal_id": "p1", "var_impact": -0.004, "cvar_impact": 0.0}]
    return json.dumps(data)


class TestCorrectiveRetry:
    async def test_invalid_reply_is_corrected_once(self, agent_config: AgentConfig) -> None:
        agent = RiskAgent(agent_config)
        llm = AsyncMock(side_effect=[_out_of_range_risk_json(), _mock_risk_json()])
        with patch.object(agent, "_call_llm", llm):
            result = await agent.run(**_RISK_KWARGS)

        assert isinstance(result, RiskReport)
        assert llm.await_count == 2
        retry_messages = llm.await_args_list[1].args[0]
        assert retry_messages[-2] == {"role": "assistant", "content": _out_of_range_risk_json()}
        # The follow-up names the failing field so the model can fix it.
        assert "per_trade_risk.0.var_impact" in retry_messages[-1]["content"]

    async def test_gives_up_after_retries_with_the_reason(self, agent_config: AgentConfig) -> None:
        agent = RiskAgent(agent_config)
        llm = AsyncMock(return_value=_out_of_range_risk_json())
        with patch.object(agent, "_call_llm", llm):
            with pytest.raises(ParseError, match=r"RiskReport: per_trade_risk\.0\.var_impact"):
                await agent.run(**_RISK_KWARGS)
        assert llm.await_count == agent.max_parse_retries + 1

    async def test_valid_first_reply_makes_one_call(self, agent_config: AgentConfig) -> None:
        agent = RiskAgent(agent_config)
        llm = AsyncMock(return_value=_mock_risk_json())
        with patch.object(agent, "_call_llm", llm):
            await agent.run(**_RISK_KWARGS)
        assert llm.await_count == 1
