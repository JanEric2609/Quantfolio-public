"""Orchestrators for the Multi-Agent Council.

This module wires the five council agents into reusable pipelines:

- `CouncilOrchestrator` runs the four-agent council for a single portfolio:
  Analyst + Macro in parallel, then Risk (which consumes the analyst output),
  then the Portfolio Manager (which consumes all three reports).

- `CompetitionOrchestrator` runs two full councils in parallel and then feeds
  both portfolios' reports into a `DebateAgent` for an adversarial review.

Both orchestrators are fully async, reuse a single `httpx.AsyncClient`, and
report per-agent errors without failing the whole pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import httpx

from app.decision.llm_portfolio.agents.analyst import AnalystAgent
from app.decision.llm_portfolio.agents.base import AgentError
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
    RiskReport,
)
from app.decision.llm_portfolio.agents.risk import RiskAgent

logger = logging.getLogger(__name__)


def create_default_config(
    portfolio_id: str,
    strategy_prompt: str,
    llm_endpoint: str,
    llm_model: str,
) -> AgentConfig:
    """Create a default `AgentConfig` for a council agent.

    Convenience helper that fills in sensible defaults for temperature, token
    budget and context window while leaving the caller in control of the
    portfolio identity, strategy prompt, and LLM endpoint.
    """
    return AgentConfig(
        portfolio_id=portfolio_id,
        strategy_prompt=strategy_prompt,
        llm_endpoint=llm_endpoint,
        llm_model=llm_model,
    )


def _opportunities_to_proposals(
    opportunities: list[Any],
    portfolio_id: str,
) -> list[dict[str, Any]]:
    """Map AnalystAgent opportunities to RiskAgent proposed_trades."""
    proposals: list[dict[str, Any]] = []
    for opp in opportunities:
        asset_id = getattr(opp, "asset_id", None)
        action = getattr(opp, "action", None)
        if asset_id is None or action is None:
            continue
        proposals.append(
            {
                "idempotency_key": f"{portfolio_id}:{asset_id}:{action}",
                "asset_id": asset_id,
                "symbol": getattr(opp, "symbol", asset_id),
                "action": str(action),
                "score": getattr(opp, "score", 50.0),
                "rationale": getattr(opp, "rationale", ""),
            }
        )
    return proposals


class CouncilOrchestrator:
    """Run the 4-agent council for a single portfolio.

    Execution order:
        1. AnalystAgent and MacroAgent run in parallel.
        2. RiskAgent runs sequentially, using the analyst opportunities as
           proposed trades.
        3. PortfolioManagerAgent runs sequentially with all three reports.

    Errors are collected per agent. A failing agent produces a default/empty
    report so downstream agents can still run with partial context.
    """

    def __init__(
        self,
        config_a: AgentConfig,
        config_r: AgentConfig,
        config_m: AgentConfig,
        config_pm: AgentConfig,
        client: httpx.AsyncClient | None = None,
    ):
        self.config_a = config_a
        self.config_r = config_r
        self.config_m = config_m
        self.config_pm = config_pm
        self.client = client

        self.analyst_agent = AnalystAgent(config_a, client)
        self.risk_agent = RiskAgent(config_r, client)
        self.macro_agent = MacroAgent(config_m, client)
        self.manager_agent = PortfolioManagerAgent(config_pm, client)

    async def _run_agent(
        self,
        agent: Any,
        label: str,
        default_factory: Any,
        **context: Any,
    ) -> tuple[Any, str | None]:
        """Run a single agent and return its output or a default on error."""
        try:
            return await agent.run(**context), None
        except AgentError as exc:
            logger.warning("%s failed: %s", label, exc)
            return default_factory(), f"{label}: {exc}"
        except Exception as exc:  # noqa: BLE001 - orchestrator must be resilient
            logger.exception("Unexpected error in %s", label)
            return default_factory(), f"{label}: unexpected {type(exc).__name__}: {exc}"

    async def run_council(self, **context: Any) -> CouncilResult:
        """Execute the full council pipeline for one portfolio.

        The caller supplies all agent-specific context as keyword arguments.
        Required/expected keys are the union of the agent `_build_user_message`
        signatures:

        - AnalystAgent: portfolio_id, holdings, cash, market_data, performance,
          discover_items
        - MacroAgent: portfolio_id, macro_indicators, sector_performance,
          hmm_regime, risk_free_rate
        - RiskAgent: portfolio_id, holdings, cash, var_95, cvar_95,
          max_drawdown, concentration, correlation_matrix, risk_budget
        - PortfolioManagerAgent: portfolio_id, current_state, debate_feedback,
          strategy
        """
        portfolio_id = context.get("portfolio_id", self.config_a.portfolio_id)

        # Phase 1: Analyst and Macro run in parallel.
        analysis_task = self._run_agent(
            self.analyst_agent,
            "AnalystAgent",
            lambda: AnalysisReport(portfolio_id=portfolio_id),
            portfolio_id=portfolio_id,
            holdings=context.get("holdings", {}),
            cash=context.get("cash", {}),
            market_data=context.get("market_data", {}),
            performance=context.get("performance", {}),
            discover_items=context.get("discover_items", []),
        )
        macro_task = self._run_agent(
            self.macro_agent,
            "MacroAgent",
            lambda: MacroReport(portfolio_id=portfolio_id),
            portfolio_id=portfolio_id,
            macro_indicators=context.get("macro_indicators", {}),
            sector_performance=context.get("sector_performance", {}),
            hmm_regime=context.get("hmm_regime"),
            risk_free_rate=context.get("risk_free_rate"),
        )

        (analysis, analysis_err), (macro, macro_err) = await asyncio.gather(
            analysis_task, macro_task
        )

        # Phase 2: Risk runs after Analyst finishes so it can review the
        # proposed opportunities.
        proposed_trades = _opportunities_to_proposals(
            analysis.opportunities, portfolio_id
        )
        risk, risk_err = await self._run_agent(
            self.risk_agent,
            "RiskAgent",
            lambda: RiskReport(
                portfolio_id=portfolio_id,
                var_95=0.0,
                cvar_95=0.0,
                max_drawdown=0.0,
            ),
            portfolio_id=portfolio_id,
            holdings=context.get("holdings", {}),
            cash=context.get("cash", {}),
            var_95=context.get("var_95", 0.0),
            cvar_95=context.get("cvar_95", 0.0),
            max_drawdown=context.get("max_drawdown", 0.0),
            concentration=context.get("concentration", {}),
            correlation_matrix=context.get("correlation_matrix", {}),
            proposed_trades=proposed_trades,
            risk_budget=context.get("risk_budget", 1.0),
        )

        # Phase 3: Portfolio Manager synthesises all three reports.
        manager_context = {
            "portfolio_id": portfolio_id,
            "analysis": analysis.model_dump() if analysis else {},
            "risk": risk.model_dump() if risk else {},
            "macro": macro.model_dump() if macro else {},
            "current_state": context.get("current_state", {}),
            "debate_feedback": context.get(
                "debate_feedback", "No debate feedback provided."
            ),
            "strategy": context.get("strategy", self.config_pm.strategy_prompt),
        }
        decision, decision_err = await self._run_agent(
            self.manager_agent,
            "PortfolioManagerAgent",
            lambda: DecisionReport(portfolio_id=portfolio_id),
            **manager_context,
        )

        errors = [e for e in [analysis_err, macro_err, risk_err, decision_err] if e]

        return CouncilResult(
            portfolio_id=portfolio_id,
            analysis=analysis or AnalysisReport(portfolio_id=portfolio_id),
            risk=risk
            or RiskReport(
                portfolio_id=portfolio_id,
                var_95=0.0,
                cvar_95=0.0,
                max_drawdown=0.0,
            ),
            macro=macro or MacroReport(portfolio_id=portfolio_id),
            decision=decision or DecisionReport(portfolio_id=portfolio_id),
            errors=errors,
        )


class CompetitionOrchestrator:
    """Run two councils head-to-head and moderate a debate between them.

    Each portfolio is evaluated by its own `CouncilOrchestrator`. The resulting
    analysis, risk, macro, and decision reports are then passed to a
    `DebateAgent` for an adversarial review under the provided competition
    rules.
    """

    def __init__(
        self,
        config_a: AgentConfig,
        config_b: AgentConfig,
        debate_config: AgentConfig,
        client: httpx.AsyncClient | None = None,
    ):
        self.config_a = config_a
        self.config_b = config_b
        self.debate_config = debate_config
        self.client = client

        self.debate_agent = DebateAgent(debate_config, client)

    async def run_competition(
        self,
        context_a: dict[str, Any],
        context_b: dict[str, Any],
        competition_rules: str,
        run_id: str,
    ) -> CompetitionCouncilResult:
        """Run two councils in parallel and then moderate a debate."""
        portfolio_a_id = context_a.get("portfolio_id", self.config_a.portfolio_id)
        portfolio_b_id = context_b.get("portfolio_id", self.config_b.portfolio_id)

        council_a = CouncilOrchestrator(
            self.config_a.model_copy(),  # analyst
            self.config_a.model_copy(),  # risk
            self.config_a.model_copy(),  # macro
            self.config_a.model_copy(),  # portfolio manager
            self.client,
        )
        council_b = CouncilOrchestrator(
            self.config_b.model_copy(),  # analyst
            self.config_b.model_copy(),  # risk
            self.config_b.model_copy(),  # macro
            self.config_b.model_copy(),  # portfolio manager
            self.client,
        )

        council_a_task = council_a.run_council(**context_a)
        council_b_task = council_b.run_council(**context_b)

        result_a, result_b = await asyncio.gather(council_a_task, council_b_task)

        errors: list[str] = []
        errors.extend(result_a.errors)
        errors.extend(result_b.errors)

        debate_context = {
            "portfolio_a_id": portfolio_a_id,
            "portfolio_b_id": portfolio_b_id,
            "decision_a": result_a.decision.model_dump() if result_a.decision else {},
            "decision_b": result_b.decision.model_dump() if result_b.decision else {},
            "analysis_a": result_a.analysis.model_dump() if result_a.analysis else {},
            "analysis_b": result_b.analysis.model_dump() if result_b.analysis else {},
            "risk_a": result_a.risk.model_dump() if result_a.risk else {},
            "risk_b": result_b.risk.model_dump() if result_b.risk else {},
            "macro_a": result_a.macro.model_dump() if result_a.macro else {},
            "macro_b": result_b.macro.model_dump() if result_b.macro else {},
            "competition_rules": competition_rules,
        }

        try:
            debate = cast(DebateReport, await self.debate_agent.run(**debate_context))
        except AgentError as exc:
            logger.warning("DebateAgent failed: %s", exc)
            errors.append(f"DebateAgent: {exc}")
            debate = DebateReport(
                portfolio_a_id=portfolio_a_id,
                portfolio_b_id=portfolio_b_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in DebateAgent")
            errors.append(f"DebateAgent: unexpected {type(exc).__name__}: {exc}")
            debate = DebateReport(
                portfolio_a_id=portfolio_a_id,
                portfolio_b_id=portfolio_b_id,
            )

        return CompetitionCouncilResult(
            run_id=run_id,
            portfolio_a=result_a,
            portfolio_b=result_b,
            debate=debate,
            errors=errors,
        )
