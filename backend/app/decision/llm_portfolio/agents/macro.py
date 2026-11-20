"""Macro Agent for the Multi-Agent Council.

The MacroAgent acts as a top-down macro strategist. It consumes macro
indicators, sector performance, and an optional pre-computed regime label
(e.g. from an external HMM model) and produces a structured `MacroReport`
with regime classification, risk stance, sector-rotation weights, and a
macro narrative.
"""

from __future__ import annotations

from typing import Any

from app.decision.llm_portfolio.agents.base import BaseAgent
from app.decision.llm_portfolio.agents.models import MacroReport


class MacroAgent(BaseAgent):
    """Top-down macro strategist agent.

    Outputs a `MacroReport` containing the current market regime, risk stance,
    recommended sector weights, and an explanatory macro narrative.
    """

    output_model: type[MacroReport] = MacroReport

    system_prompt: str = (
        "You are a disciplined macro strategist on a portfolio-management council. "
        "Your job is to determine the current market regime, adopt a risk stance, "
        "and recommend sector rotation weights.\n\n"
        "Use the macro indicators and sector performance data provided in the user "
        "message. If a pre-computed regime label is supplied, treat it as a strong "
        "prior but apply your own judgment based on the data.\n\n"
        "Classify the regime as one of: expansion, contraction, crisis, unknown. "
        "Choose a risk stance of: risk_on, risk_off, or neutral. "
        "Set regime_confidence as a float between 0 and 1.\n\n"
        "Provide sector_weights as a JSON list of objects, each with:\n"
        "  sector: sector name (e.g. Technology, Energy, Healthcare, Financials)\n"
        "  weight: recommended allocation fraction between 0 and 1\n"
        "  rationale: concise reason for the weight\n"
        "The sector weights should sum to 1.0 (or very close to it).\n\n"
        "Write a concise macro_narrative that explains the regime call, risk stance, "
        "and key sector over/under-weights in 2-4 sentences.\n\n"
        "Return a single JSON object with exactly this structure and no extra text:\n"
        "{\n"
        '  "portfolio_id": "...",\n'
        '  "regime": "expansion|contraction|crisis|unknown",\n'
        '  "risk_stance": "risk_on|risk_off|neutral",\n'
        '  "regime_confidence": 0.0,\n'
        '  "sector_weights": [\n'
        '    {"sector": "...", "weight": 0.0, "rationale": "..."}\n'
        "  ],\n"
        '  "macro_narrative": "...",\n'
        '  "risk_budget_adjustment": 0.0\n'
        "}\n\n"
        "risk_budget_adjustment must be a float between -0.5 and +0.5, representing "
        "a recommended additive adjustment to the portfolio's risk budget. "
        "Positive values increase risk exposure; negative values reduce it."
    )

    def _build_user_message(self, **context: Any) -> str:
        """Format macro context into a user message for the LLM.

        Expected context keys:
            portfolio_id: str
            macro_indicators: dict (e.g. vix, yield_curve, inflation, gdp_growth)
            sector_performance: dict (sector -> performance metric)
            hmm_regime: optional[str] pre-computed regime label
            risk_free_rate: float
        """
        portfolio_id: str = context.get("portfolio_id", "")
        macro_indicators: dict[str, Any] = context.get("macro_indicators") or {}
        sector_performance: dict[str, Any] = context.get("sector_performance") or {}
        hmm_regime: str | None = context.get("hmm_regime")
        risk_free_rate: float | None = context.get("risk_free_rate")

        lines: list[str] = [
            "# Macro Analysis Request",
            "",
            f"portfolio_id: {portfolio_id}",
            "",
            "## Macro Indicators",
            self._json_block(macro_indicators),
            "",
            "## Sector Performance",
            self._json_block(sector_performance),
            "",
        ]

        if hmm_regime is not None:
            lines.extend(
                [
                    "## Pre-computed Regime Prior (external HMM)",
                    f"hmm_regime: {hmm_regime}",
                    "",
                ]
            )

        if risk_free_rate is not None:
            lines.extend(
                [
                    "## Risk-Free Rate",
                    f"risk_free_rate: {risk_free_rate}",
                    "",
                ]
            )

        lines.extend(
            [
                "## Instructions",
                "Return ONLY a JSON object matching the MacroReport structure.",
                "Do not include markdown formatting, code fences, or explanatory text outside the JSON object.",
            ]
        )

        return "\n".join(lines)
