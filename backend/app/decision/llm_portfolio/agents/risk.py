"""RiskAgent for the Multi-Agent Council.

The RiskAgent acts as the council's risk manager. It receives pre-computed,
deterministic risk metrics from Python quant code and uses the LLM to interpret
those numbers, identify concentration and correlation concerns, evaluate the
risk impact of proposed trades, and produce a structured RiskReport.

All arithmetic (VaR, CVaR, drawdown, concentration, correlation) is performed
outside this agent. The agent's job is judgment, narrative, and structured
assessment — not calculation.
"""

from __future__ import annotations

import json
from typing import Any

from app.decision.llm_portfolio.agents.base import BaseAgent
from app.decision.llm_portfolio.agents.models import RiskReport


class RiskAgent(BaseAgent):
    """Risk manager agent: evaluates portfolio risk and proposed trades."""

    output_model: type[RiskReport] = RiskReport

    system_prompt: str = (
        "You are the Risk Manager of a Multi-Agent Portfolio Council. "
        "Your job is to protect the portfolio from unacceptable losses while "
        "still allowing sensible trades to pass through.\n\n"
        "You are supplied with pre-computed, deterministic risk numbers: "
        "portfolio-level 1-day 95% VaR and CVaR, maximum drawdown, "
        "concentration metrics, a correlation matrix, and the remaining risk "
        "budget. Do NOT recompute these values. Interpret them, explain their "
        "meaning, and decide whether the proposed trades from the Analyst "
        "fit within the risk budget.\n\n"
        "For every proposed trade you must produce a TradeRisk object with:\n"
        "- proposal_id: matches the incoming trade's idempotency_key\n"
        "- var_impact: incremental VaR contribution (loss magnitude, >= 0)\n"
        "- cvar_impact: incremental CVaR contribution (>= 0)\n"
        "- concentration_risk: post-trade concentration level, 0 (safe) to 1 (extreme)\n"
        "- correlation_warning: a short warning if the trade increases correlation risk, or null\n"
        "- risk_score: aggregate 0 (low risk) to 1 (high risk)\n\n"
        "Also produce:\n"
        "- concentration_warnings: list of human-readable strings for any position or sector that exceeds risk tolerance\n"
        "- correlation_warnings: list of human-readable strings for highly correlated pairs\n"
        "- risk_budget_remaining: fraction of the risk budget still available after proposed trades, 0 to 1\n"
        "- narrative: a concise risk assessment summary written for a portfolio manager\n\n"
        "Rules:\n"
        "1. Be conservative but not paranoid: flag real risks, do not invent them.\n"
        "2. If VaR or CVaR is zero or missing, state that explicitly rather than guessing.\n"
        "3. If concentration data is empty, report no concentration warnings.\n"
        "4. If correlation_matrix is empty, report no correlation warnings.\n"
        "5. Risk budget remaining must decrease as more risky trades are proposed.\n"
        "6. Return ONLY valid JSON matching the requested RiskReport schema.\n"
    )

    def _build_user_message(self, **context: Any) -> str:
        """Format deterministic risk metrics and proposed trades for the LLM.

        Expected context keys:
            portfolio_id: str
            holdings: dict
            cash: dict
            var_95: float
            cvar_95: float
            max_drawdown: float
            concentration: dict
            correlation_matrix: dict
            proposed_trades: list[dict]
            risk_budget: float
        """
        portfolio_id = context.get("portfolio_id", "unknown")
        holdings = context.get("holdings", {})
        cash = context.get("cash", {})
        var_95 = context.get("var_95", 0.0)
        cvar_95 = context.get("cvar_95", 0.0)
        max_drawdown = context.get("max_drawdown", 0.0)
        concentration = context.get("concentration", {})
        correlation_matrix = context.get("correlation_matrix", {})
        proposed_trades = context.get("proposed_trades", [])
        risk_budget = context.get("risk_budget", 1.0)

        parts: list[str] = [
            "You are performing a portfolio risk review.",
            "",
            f"Portfolio ID: {portfolio_id}",
            "",
            self._format_holdings(holdings),
            "",
            self._format_cash(cash),
            "",
            "Pre-computed Risk Metrics (do not recompute these):",
            f"  1-day 95% VaR: {var_95:.6f}",
            f"  1-day 95% CVaR: {cvar_95:.6f}",
            f"  Max Drawdown: {max_drawdown:.6f}",
            f"  Risk Budget (total): {risk_budget:.6f}",
            "",
            "Concentration Profile:",
        ]

        if concentration:
            parts.append(self._json_block(concentration))
        else:
            parts.append("  No concentration data provided.")

        parts.extend([
            "",
            "Correlation Matrix:",
        ])

        if correlation_matrix:
            parts.append(self._json_block(correlation_matrix))
        else:
            parts.append("  No correlation matrix provided.")

        parts.extend([
            "",
            "Proposed Trades (from Analyst):",
        ])

        if proposed_trades:
            parts.append(self._json_block(proposed_trades))
        else:
            parts.append("  No proposed trades.")

        parts.extend([
            "",
            "Instructions:",
            "1. Review the pre-computed metrics above.",
            "2. For each proposed trade, estimate var_impact, cvar_impact, concentration_risk, and risk_score.",
            "3. Identify any concentration warnings (e.g., single position > 10% of portfolio) and correlation warnings (e.g., pairwise correlation > 0.8).",
            "4. Compute risk_budget_remaining as the fraction of the risk budget left after the proposed trades.",
            "5. Write a concise narrative summarizing the risk picture.",
            "6. Return ONLY a single JSON object matching this exact schema:",
            "",
            json.dumps({
                "portfolio_id": portfolio_id,
                "var_95": 0.0,
                "cvar_95": 0.0,
                "max_drawdown": 0.0,
                "concentration_warnings": ["string"],
                "correlation_warnings": ["string"],
                "per_trade_risk": [
                    {
                        "proposal_id": "string",
                        "var_impact": 0.0,
                        "cvar_impact": 0.0,
                        "concentration_risk": 0.0,
                        "correlation_warning": "string or null",
                        "risk_score": 0.0,
                    }
                ],
                "risk_budget_remaining": 0.0,
                "narrative": "string",
            }, indent=2),
        ])

        return "\n".join(parts)
