"""Multi-Agent Council — Phase 2 (Dual Competition System).

5 agents per portfolio: Analyst, Risk, Macro, PortfolioManager, Debate.
Plus orchestration: CouncilOrchestrator (single portfolio), CompetitionOrchestrator (both portfolios + debate).
"""

from __future__ import annotations

from app.decision.llm_portfolio.agents.analyst import AnalystAgent
from app.decision.llm_portfolio.agents.base import AgentError, BaseAgent, ParseError, LLMCallError
from app.decision.llm_portfolio.agents.debate import DebateAgent
from app.decision.llm_portfolio.agents.macro import MacroAgent
from app.decision.llm_portfolio.agents.manager import PortfolioManagerAgent
from app.decision.llm_portfolio.agents.models import (
    AgentConfig,
    AnalysisReport,
    CompetitionCouncilResult,
    CouncilResult,
    DebateReport,
    DecisionReport,
    MacroReport,
    Opportunity,
    RegimeLabel,
    RiskFlag,
    RiskReport,
    RiskStance,
    SectorAllocation,
    TradeProposalItem,
    TradeRisk,
)
from app.decision.llm_portfolio.agents.orchestrator import (
    CompetitionOrchestrator,
    CouncilOrchestrator,
    create_default_config,
)
from app.decision.llm_portfolio.agents.risk import RiskAgent

__all__ = [
    # Agents
    "BaseAgent",
    "AnalystAgent",
    "RiskAgent",
    "MacroAgent",
    "PortfolioManagerAgent",
    "DebateAgent",
    # Orchestration
    "CouncilOrchestrator",
    "CompetitionOrchestrator",
    "create_default_config",
    # Models
    "AgentConfig",
    "AnalysisReport",
    "CompetitionCouncilResult",
    "CouncilResult",
    "DebateReport",
    "DecisionReport",
    "MacroReport",
    "Opportunity",
    "RegimeLabel",
    "RiskFlag",
    "RiskReport",
    "RiskStance",
    "SectorAllocation",
    "TradeProposalItem",
    "TradeRisk",
    # Exceptions
    "AgentError",
    "LLMCallError",
    "ParseError",
]
