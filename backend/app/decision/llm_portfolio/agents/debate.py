"""DebateAgent for the Multi-Agent Council.

Acts as an adversarial moderator that critically reviews two competing
portfolio decisions side-by-side, identifies weaknesses, and assigns revised
confidence scores and suggested trade revisions.
"""

from __future__ import annotations

import json
from typing import Any

from app.decision.llm_portfolio.agents.base import BaseAgent
from app.decision.llm_portfolio.agents.models import DebateReport


class DebateAgent(BaseAgent):
    """Adversarial debate moderator for dual-portfolio competition.

    Compares the decision reports of portfolio A and portfolio B, interrogates
    their underlying analysis, risk, and macro reports, and produces a
    structured critique with revised confidence scores and concrete trade
    revisions.
    """

    output_model: type[DebateReport] = DebateReport

    system_prompt: str = (
        "You are a skeptical, adversarial debate moderator reviewing two "
        "portfolio managers (A and B) that have just submitted their final "
        "trade decisions for the same investment mandate and macro environment.\n\n"
        "Your goal is to find weaknesses, contradictions, blind spots, and "
        "over-concentrations in each portfolio's decisions. You must be "
        "skeptical, precise, and willing to reduce confidence when you find "
        "faults. Do not be polite for the sake of politeness. Treat every "
        "trade as a claim that must be defended by evidence in the supporting "
        "reports.\n\n"
        "Review criteria:\n"
        "1. Contradictions: Are any trades inconsistent with the portfolio's "
        "own analysis, risk report, or macro view?\n"
        "2. Over-concentration: Does a portfolio pile into a single asset, "
        "sector, factor, or correlation cluster despite risk warnings?\n"
        "3. Macro alignment: Do the trades make sense given the macro regime, "
        "risk stance, and sector weights in the macro report?\n"
        "4. Risk budget discipline: Does the portfolio stay within its stated "
        "risk budget, VaR/CVaR limits, and drawdown tolerance?\n"
        "5. Conviction vs. evidence: Are high-confidence trades backed by "
        "strong analysis, or are they momentum-chasing / narrative-driven?\n"
        "6. Missing trades: Is there an obvious asset or hedge that a "
        "portfolio should hold but omits without justification?\n\n"
        "Confidence rules:\n"
        "- Start from each portfolio's original decision confidence.\n"
        "- Lower the revised confidence for every material weakness you find.\n"
        "- If a portfolio's decisions are coherent, well-defended, and "
        "aligned with its reports, you may keep or slightly adjust the score.\n"
        "- Never raise confidence purely because you agree with the trades; "
        "only raise it if the supporting evidence is stronger than the "
        "original report implies.\n\n"
        "Revision rules:\n"
        "- Provide concrete, actionable suggested revisions for each portfolio.\n"
        "- add_trades: list trades that should be added, with asset_id, action, "
        "quantity (optional), and rationale.\n"
        "- remove_trades: list proposed trades that should be removed or "
        "rejected, with asset_id and rationale.\n"
        "- revise_trades: list trades that should be modified (e.g., smaller "
        "size, different action), with asset_id, proposed change, and rationale.\n\n"
        "Output a single JSON object matching the DebateReport structure. "
        "Do not include markdown code fences or any text outside the JSON object."
    )

    def _build_user_message(self, **context: Any) -> str:
        """Format both portfolios' reports and competition rules for the LLM."""
        portfolio_a_id: str = context.get("portfolio_a_id", "portfolio_a")
        portfolio_b_id: str = context.get("portfolio_b_id", "portfolio_b")
        decision_a: dict[str, Any] = context.get("decision_a") or {}
        decision_b: dict[str, Any] = context.get("decision_b") or {}
        analysis_a: dict[str, Any] = context.get("analysis_a") or {}
        analysis_b: dict[str, Any] = context.get("analysis_b") or {}
        risk_a: dict[str, Any] = context.get("risk_a") or {}
        risk_b: dict[str, Any] = context.get("risk_b") or {}
        macro_a: dict[str, Any] = context.get("macro_a") or {}
        macro_b: dict[str, Any] = context.get("macro_b") or {}
        competition_rules: str = context.get("competition_rules", "")

        original_a_confidence: float = float(decision_a.get("confidence", 0.5))
        original_b_confidence: float = float(decision_b.get("confidence", 0.5))

        parts = [
            "# Dual Portfolio Debate\n",
            "## Competition Rules",
            competition_rules or "No specific competition rules provided.",
            "",
            f"## Portfolio A: {portfolio_a_id}",
            "",
            "### Original Decision Report",
            self._json_block(decision_a),
            "",
            "### Underlying Analysis Report",
            self._json_block(analysis_a),
            "",
            "### Risk Report",
            self._json_block(risk_a),
            "",
            "### Macro Report",
            self._json_block(macro_a),
            "",
            f"## Portfolio B: {portfolio_b_id}",
            "",
            "### Original Decision Report",
            self._json_block(decision_b),
            "",
            "### Underlying Analysis Report",
            self._json_block(analysis_b),
            "",
            "### Risk Report",
            self._json_block(risk_b),
            "",
            "### Macro Report",
            self._json_block(macro_b),
            "",
            "## Instructions",
            f"- Portfolio A original confidence: {original_a_confidence}",
            f"- Portfolio B original confidence: {original_b_confidence}",
            "- Produce 3 to 5 specific critiques per portfolio.",
            "- For each critique, cite the report section or trade that supports it.",
            "- Lower revised confidence when weaknesses are material.",
            "- Provide suggested_revisions using this exact shape:",
            json.dumps(
                {
                    "portfolio_a": {
                        "add_trades": [],
                        "remove_trades": [],
                        "revise_trades": [],
                    },
                    "portfolio_b": {
                        "add_trades": [],
                        "remove_trades": [],
                        "revise_trades": [],
                    },
                },
                indent=2,
            ),
            "",
            "## Required JSON Output",
            json.dumps(
                {
                    "portfolio_a_id": portfolio_a_id,
                    "portfolio_b_id": portfolio_b_id,
                    "portfolio_a_critique": ["specific critique 1", "specific critique 2"],
                    "portfolio_b_critique": ["specific critique 1", "specific critique 2"],
                    "portfolio_a_confidence": 0.0,
                    "portfolio_b_confidence": 0.0,
                    "suggested_revisions": {
                        "portfolio_a": {
                            "add_trades": [],
                            "remove_trades": [],
                            "revise_trades": [],
                        },
                        "portfolio_b": {
                            "add_trades": [],
                            "remove_trades": [],
                            "revise_trades": [],
                        },
                    },
                    "narrative": "Concise debate summary and verdict.",
                },
                indent=2,
            ),
        ]
        return "\n".join(parts)
