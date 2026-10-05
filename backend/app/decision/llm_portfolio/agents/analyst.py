"""AnalystAgent for the Multi-Agent Council.

The AnalystAgent acts as a quantitative portfolio analyst. It consumes the
current portfolio state, market data, performance metrics, and an optional
discovery shortlist, then produces an `AnalysisReport` containing ranked
investment opportunities, risk flags, and a concise market narrative.

All LLM arithmetic is advisory only — deterministic calculations live in the
portfolio and quant services. Reasoning is disabled per Machine Spirits
mitigation constraints.
"""

from __future__ import annotations

from typing import Any

from app.decision.llm_portfolio.agents.base import BaseAgent
from app.decision.llm_portfolio.agents.models import AnalysisReport


class AnalystAgent(BaseAgent):
    """Quantitative analyst that identifies opportunities and risks.

    Outputs a validated `AnalysisReport` with opportunities, risk flags,
    market narrative, and overall confidence.
    """

    output_model: type[AnalysisReport] = AnalysisReport

    system_prompt: str = (
        "You are AnalystAgent, a disciplined quantitative portfolio analyst in a "
        "Multi-Agent Council. Your job is to evaluate the current portfolio state "
        "and market data, identify a small set of actionable opportunities, flag "
        "material risks, and write a concise market narrative.\n\n"
        "ROLE AND CONSTRAINTS:\n"
        "- Be objective, evidence-driven, and concise.\n"
        "- Do not enable reasoning, chain-of-thought, or internal monologue. "
        "Output only the requested JSON.\n"
        "- Recommend only 'buy' or 'sell' actions (no neutral/no-op opportunities).\n"
        "- Produce at most 3-5 opportunities and at most 3-5 risk flags.\n"
        "- Score opportunities from 0 (unattractive) to 100 (highly attractive).\n"
        "- Set risk severity from 0 (benign) to 1 (critical).\n"
        "- Set overall confidence from 0 (uncertain) to 1 (highly confident).\n\n"
        "JSON SCHEMA:\n"
        "{\n"
        '  "portfolio_id": "<portfolio identifier>",\n'
        '  "opportunities": [\n'
        "    {\n"
        '      "asset_id": "<ISIN or symbol>",\n'
        '      "symbol": "<display ticker>",\n'
        '      "action": "buy" | "sell",\n'
        '      "score": <0.0-100.0>,\n'
        '      "rationale": "<1-2 sentence justification>"\n'
        "    }\n"
        "  ],\n"
        '  "risk_flags": [\n'
        "    {\n"
        '      "asset_id": "<ISIN or symbol>",\n'
        '      "symbol": "<display ticker>",\n'
        '      "risk_type": "concentration" | "volatility" | "drawdown" | "correlation" | "liquidity" | "sentiment",\n'
        '      "severity": <0.0-1.0>,\n'
        '      "description": "<concise risk description>"\n'
        "    }\n"
        "  ],\n"
        '  "market_narrative": "<brief synthesis of market conditions and implications>",\n'
        '  "confidence": <0.0-1.0>\n'
        "}\n\n"
        "Return ONLY valid JSON matching the schema above. Do not wrap the JSON in "
        "markdown code fences or add explanatory text outside the JSON object."
    )

    def _build_user_message(self, **context: Any) -> str:
        """Build the user message from portfolio context.

        Expected context keys:
        - portfolio_id: str
        - holdings: dict
        - cash: dict
        - market_data: dict (prices, returns, etc.)
        - performance: dict
        - discover_items: optional list of short-listed assets
        """
        portfolio_id: str = context.get("portfolio_id", "unknown")
        holdings: dict[str, Any] = context.get("holdings", {}) or {}
        cash: dict[str, Any] = context.get("cash", {}) or {}
        market_data: dict[str, Any] = context.get("market_data", {}) or {}
        performance: dict[str, Any] = context.get("performance", {}) or {}
        discover_items: list[Any] = context.get("discover_items", []) or []

        lines: list[str] = [
            f"Portfolio ID: {portfolio_id}",
            "",
            "Analyze the following portfolio state and market information. "
            "Return ONLY a JSON object with exactly this structure:",
            "",
            '{"opportunities": [...], "risk_flags": [...], "market_narrative": "...", "portfolio_id": "...", "confidence": 0.0}',
            "",
            self._format_holdings(holdings),
            "",
            self._format_cash(cash),
            "",
            "Market Data:",
            self._json_block(market_data),
            "",
            "Performance Metrics:",
            self._json_block(performance),
            "",
        ]

        if discover_items:
            lines.extend([
                "Discovery Shortlist:",
                self._json_block(discover_items),
                "",
            ])
        else:
            lines.extend([
                "Discovery Shortlist: None",
                "",
            ])

        lines.extend([
            "INSTRUCTIONS:",
            "- Identify 3-5 concrete buy/sell opportunities with scores and rationale.",
            "- Flag 3-5 material risks with severity and description.",
            "- Write a concise market narrative (2-4 sentences).",
            "- Set confidence based on data quality and clarity of signals.",
            "- Do not explain your reasoning; return only the JSON object.",
        ])

        return "\n".join(lines)
