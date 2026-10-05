"""Portfolio Manager agent for the Multi-Agent Council.

The ManagerAgent is the final decision maker in the council pipeline. It
consumes the AnalystReport, RiskReport, and MacroReport for a portfolio and
emits a DecisionReport containing concrete trade proposals, target weights,
risk-budget allocation, and Black-Litterman views.

Design notes:
- This agent proposes views and allocations only; it does NOT run portfolio
  optimization or Black-Litterman math (those are downstream engine concerns).
- All arithmetic stays out of the LLM: the model returns structured data that
  deterministic Python code validates and consumes.
"""

from __future__ import annotations

from typing import Any

from app.decision.llm_portfolio.agents.base import BaseAgent
from app.decision.llm_portfolio.agents.models import DecisionReport


class PortfolioManagerAgent(BaseAgent):
    """Synthesize analysis, risk, and macro reports into a DecisionReport."""

    output_model: type[DecisionReport] = DecisionReport

    system_prompt: str = (
        "You are the PortfolioManagerAgent in a Multi-Agent Council. "
        "Your job is to act as a disciplined portfolio manager and synthesize "
        "three council reports into a single, actionable DecisionReport for one "
        "portfolio.\n\n"
        "Inputs you will receive:\n"
        "1. AnalysisReport — ranked opportunities and risk flags from the AnalystAgent.\n"
        "2. RiskReport — portfolio-level VaR/CVaR, drawdown, concentration warnings, "
        "   per-trade risk assessments, and remaining risk budget.\n"
        "3. MacroReport — detected market regime, risk stance, sector allocation "
        "   guidance, and macro narrative.\n"
        "4. Current portfolio state — holdings and cash balances.\n"
        "5. Strategy instructions — the investing style/mandate for this portfolio.\n"
        "6. Optional debate feedback — adversarial critique from the DebateAgent.\n\n"
        "Your responsibilities:\n"
        "- Prioritize the highest-conviction opportunities while respecting risk limits.\n"
        "- Reduce or exit positions when risk flags, concentration warnings, or the "
        "  macro stance demand it.\n"
        "- Translate sector/theme guidance from the MacroReport into target weights "
        "  and risk-budget fractions.\n"
        "- Produce Black-Litterman views as small-magnitude relative return "
        "  expectations only; do NOT perform Black-Litterman optimization.\n\n"
        "Decision constraints:\n"
        "- Propose at most 10 trades.\n"
        "- Each trade proposal must contain: asset_id, action ('buy' or 'sell'), "
        "  quantity (number of units/shares), price (estimated execution price), "
        "rationale (1-2 sentences).\n"
        "- target_weights maps asset_id to a fraction of total portfolio value. "
        "  The sum of all target_weights must be <= 1.0; the remainder is cash.\n"
        "- risk_budget_allocation maps category names (sector, theme, or asset class) "
        "  to fractions of the risk budget. All values >= 0; total <= 1.0.\n"
        "- blm_views maps asset_id to a relative expected return view for "
        "  Black-Litterman. Values must be between -0.1 and +0.1.\n"
        "- strategy_narrative explains the overall posture and reasoning.\n"
        "- confidence is the overall decision confidence (0.0-1.0).\n"
        "- Respond with valid JSON only. No markdown fences, no extra commentary."
    )

    def _build_user_message(self, **context: Any) -> str:
        """Assemble the user prompt from council context.

        Expected context keys:
            portfolio_id: str
            analysis: AnalysisReport or dict
            risk: RiskReport or dict
            macro: MacroReport or dict
            current_state: dict with 'holdings' and 'cash'
            debate_feedback: optional str
            strategy: optional str (falls back to AgentConfig.strategy_prompt)
        """
        portfolio_id = context.get("portfolio_id", self.config.portfolio_id)
        strategy = context.get("strategy") or self.config.strategy_prompt
        debate_feedback = context.get("debate_feedback") or "No debate feedback provided."

        analysis = context.get("analysis", {})
        risk = context.get("risk", {})
        macro = context.get("macro", {})

        current_state = context.get("current_state", {}) or {}
        if isinstance(current_state, dict):
            holdings = current_state.get("holdings", {})
            cash = current_state.get("cash", {})
        else:
            holdings = {}
            cash = {}

        parts = [
            f"Portfolio ID: {portfolio_id}",
            "",
            "=== STRATEGY INSTRUCTIONS ===",
            strategy,
            "",
            "=== ANALYSIS REPORT ===",
            self._json_block(analysis),
            "",
            "=== RISK REPORT ===",
            self._json_block(risk),
            "",
            "=== MACRO REPORT ===",
            self._json_block(macro),
            "",
            "=== CURRENT PORTFOLIO STATE ===",
            self._format_holdings(holdings),
            "",
            self._format_cash(cash),
            "",
            "=== DEBATE FEEDBACK ===",
            debate_feedback,
            "",
            "=== REQUIRED OUTPUT ===",
            "Return a single JSON object matching this DecisionReport structure exactly:",
            "{",
            f'  "portfolio_id": "{portfolio_id}",',
            '  "trade_proposals": [',
            '    {',
            '      "asset_id": "string",',
            '      "action": "buy" | "sell",',
            '      "quantity": number,',
            '      "price": number,',
            '      "rationale": "string",',
            '      "idempotency_key": "string"',
            '    }',
            '  ],',
            '  "target_weights": {"asset_id": number, ...},',
            '  "risk_budget_allocation": {"category": number, ...},',
            '  "blm_views": {"asset_id": number, ...},',
            '  "strategy_narrative": "string",',
            '  "confidence": number',
            "}",
            "",
            "Rules:",
            "- trade_proposals may contain at most 10 items.",
            "- target_weights values must sum to <= 1.0 (cash absorbs the remainder).",
            "- risk_budget_allocation values must be >= 0 and sum to <= 1.0.",
            "- blm_views values must be in the range [-0.1, +0.1].",
            "- Do NOT perform Black-Litterman optimization; only provide views.",
            "- Output JSON only, with no markdown code fences.",
        ]

        return "\n".join(parts)
