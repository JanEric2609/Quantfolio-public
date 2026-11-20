"""LLM Research Agent with Financial Chain-of-Thought prompting.

Uses the local LLM router to generate research-grade factor proposals,
market analysis, and regime-transition evaluations. Integrates with
existing regime context, holdings data, and factor library.

All outputs are estimates and not investment advice.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.llm.router import call as llm_call

logger = logging.getLogger(__name__)


def _safe_confidence(val: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(val)))
    except (TypeError, ValueError):
        return default


@dataclass
class ResearchContext:
    """Bundled context for LLM research calls."""

    regime_label: str = "sideways"
    regime_confidence: float = 0.5
    crisis: bool = False
    existing_factor_names: list[str] = field(default_factory=list)
    news_headlines: list[str] = field(default_factory=list)
    market_summary: dict[str, Any] = field(default_factory=dict)
    holdings_summary: dict[str, Any] = field(default_factory=dict)
    factor_scores: dict[str, float] = field(default_factory=dict)

    def to_prompt_context(self) -> str:
        """Build a concise context block for the LLM prompt."""
        parts = [
            f"Regime: {self.regime_label} (confidence {self.regime_confidence:.0%})"
            + (" CRISIS ACTIVE" if self.crisis else ""),
        ]
        if self.existing_factor_names:
            parts.append(f"Known factors: {', '.join(self.existing_factor_names[:20])}")
        if self.news_headlines:
            # Third-party headlines are untrusted input; fence them (proposal P6).
            from app.foundation.llm.untrusted import wrap_untrusted

            parts.append("Recent news:\n" + wrap_untrusted("news headlines", list(self.news_headlines[:8])))
        if self.holdings_summary:
            total = self.holdings_summary.get("total_value", 0)
            n_pos = len(self.holdings_summary.get("positions", []))
            parts.append(f"Portfolio: {n_pos} positions, total value {total:,.0f}")
        if self.factor_scores:
            top_factors = sorted(self.factor_scores.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
            parts.append("Top factor exposures: " + ", ".join(f"{k}={v:.3f}" for k, v in top_factors))
        return "\n".join(parts)


@dataclass
class FactorProposal:
    """An LLM-proposed research factor."""

    name: str
    description: str
    formula: str
    intuition: str
    expected_regime: str | None = None
    confidence: float = 0.5
    related_concepts: list[str] = field(default_factory=list)


@dataclass
class MarketAnalysis:
    """LLM-generated market analysis output."""

    summary: str
    regime_assessment: str
    risks: list[str] = field(default_factory=list)
    opportunities: list[str] = field(default_factory=list)
    factor_implications: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.5


@dataclass
class RegimeTransitionEval:
    """LLM evaluation of a regime transition."""

    from_regime: str
    to_regime: str
    plausibility: str
    implications: str
    recommended_actions: list[str] = field(default_factory=list)
    factor_adjustments: dict[str, float] = field(default_factory=dict)


def _build_research_context(db: Session, user_id: str) -> ResearchContext:
    """Assemble a ResearchContext from current data."""
    ctx = ResearchContext()

    # Regime
    try:
        from app.lab.regime import get_or_refresh_regime
        regime = get_or_refresh_regime(db)
        ctx.regime_label = regime.get("label", "sideways")
        ctx.regime_confidence = float(regime.get("score", 0.5))
        ctx.crisis = bool(regime.get("crisis", False))
    except Exception:
        pass

    # Existing factor names from factors library
    try:
        from app.foundation.models.entities import FactorsLibrary
        factors = db.query(FactorsLibrary).all()
        ctx.existing_factor_names = [f.name for f in factors if f.name]
    except Exception:
        pass

    # Holdings summary
    try:
        from app.foundation.portfolio.bridge import get_real_holdings_summary
        holdings = get_real_holdings_summary(db, user_id)
        if holdings.get("total_value", 0) > 0:
            ctx.holdings_summary = holdings
    except Exception:
        pass

    # Factor exposures
    try:
        from app.foundation.portfolio.metrics_wrappers import compute_factor_exposures_real
        exposures = compute_factor_exposures_real(db, user_id)
        ctx.factor_scores = exposures.get("exposures", {})
    except Exception:
        pass

    return ctx


async def propose_factors(
    db: Session,
    user_id: str,
    n_proposals: int = 3,
    force_local: bool = False,
) -> tuple[list[FactorProposal], str]:
    """Use the LLM to propose novel factors via Financial Chain-of-Thought.

    Returns (proposals, reasoning_trace). Only factors with economic intuition
    and clear formulas are returned.
    """
    ctx = _build_research_context(db, user_id)

    # Filter out already-existing factor names
    existing = set(ctx.existing_factor_names)

    prompt = f"""You are a quantitative finance researcher using Financial Chain-of-Thought reasoning.

## Context
{ctx.to_prompt_context()}

## Existing Factors (avoid duplicates)
{', '.join(sorted(existing)) if existing else 'None yet.'}

## Task
Propose {n_proposals} novel quantitative factors that could improve portfolio construction.

For each factor, reason through:
1. **Economic intuition** — What market anomaly or behavioral bias does this capture?
2. **Regime sensitivity** — How does this factor behave in {ctx.regime_label} markets?
3. **Implementation** — What data is needed and how is it computed?
4. **Risk** — What are the failure modes and edge cases?
5. **Distinctiveness** — How is this different from standard momentum/value/size factors?

## Output Format
Respond as a JSON array of objects with keys:
- name: Short factor name (lowercase_snake_case)
- description: 1-2 sentence description
- formula: Plain-English formula description
- intuition: 1-2 sentence economic reasoning
- expected_regime: Which regime this works best in (bull/bear/sideways/any)
- confidence: Your confidence 0.0-1.0

Only output the JSON array, no other text.
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        completion = await llm_call(
            db,
            task_type="batch_research",
            messages=messages,
            force_local=force_local,
            timeout_s=120.0,
        )
        response_text = completion.content
    except Exception as exc:
        logger.warning("LLM factor proposal failed: %s", exc)
        return [], f"LLM call failed: {exc}"

    # Parse response
    proposals = _parse_factor_proposals(response_text, existing)

    reasoning = f"Financial CoT reasoning (regime={ctx.regime_label}, crisis={ctx.crisis}):\n\n"
    reasoning += response_text

    return proposals, reasoning


def _parse_factor_proposals(text: str, existing: set[str]) -> list[FactorProposal]:
    """Extract factor proposals from LLM response."""
    # Try to find JSON array in response
    start = text.find("[")
    end = text.rfind("]") + 1
    if start < 0 or end <= start:
        return []

    try:
        raw_items = json.loads(text[start:end])
    except json.JSONDecodeError:
        return []

    if not isinstance(raw_items, list):
        return []

    proposals = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower().replace(" ", "_")
        if not name or name in existing:
            continue
        proposals.append(
            FactorProposal(
                name=name,
                description=str(item.get("description", "")),
                formula=str(item.get("formula", "")),
                intuition=str(item.get("intuition", "")),
                expected_regime=item.get("expected_regime"),
                confidence=_safe_confidence(item.get("confidence", 0.5)),
                related_concepts=item.get("related_concepts") or [],
            )
        )

    return proposals


async def analyze_market(
    db: Session,
    user_id: str,
    focus: str = "general",
    force_local: bool = False,
) -> tuple[MarketAnalysis, str]:
    """Generate a market analysis with Financial Chain-of-Thought.

    Args:
        db: Database session
        user_id: User ID
        focus: Analysis focus (general, regime, factors, portfolio)
        force_local: Force local LLM backend

    Returns:
        (analysis, reasoning_trace)
    """
    ctx = _build_research_context(db, user_id)

    focus_instructions = {
        "general": "Provide a balanced overview of current market conditions and implications for a quant portfolio.",
        "regime": "Focus on regime classification: what regime are we in, is a transition likely, and what signals support this?",
        "factors": "Focus on factor performance and rotation: which factors are working, which are not, and what rotation signals exist?",
        "portfolio": "Focus on the user's portfolio: what are the key risks, how does the allocation align with the regime, and what adjustments would improve risk-adjusted returns?",
    }

    prompt = f"""You are a senior quantitative portfolio strategist performing Financial Chain-of-Thought analysis.

## Current Context
{ctx.to_prompt_context()}

## Analysis Focus
{focus_instructions.get(focus, focus_instructions['general'])}

## Reasoning Framework
Work through your analysis step by step:
1. **Market regime assessment** — What does the data say about current conditions?
2. **Factor environment** — Which factors are rewarded/penalized in this regime?
3. **Risk identification** — What are the top 3 risks right now?
4. **Opportunity identification** — Where is the market mispricing risk?
5. **Actionable implications** — What should a quant portfolio manager do?

## Output Format
Respond as a JSON object with keys:
- summary: 2-3 sentence overall assessment
- regime_assessment: Your view on the current regime
- risks: Array of top 3 risk strings
- opportunities: Array of top 2-3 opportunity strings
- factor_implications: Object mapping factor names to implications strings
- confidence: Your confidence 0.0-1.0

Only output the JSON object, no other text.
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        completion = await llm_call(
            db,
            task_type="batch_research",
            messages=messages,
            force_local=force_local,
            timeout_s=90.0,
        )
        response_text = completion.content
    except Exception as exc:
        logger.warning("LLM market analysis failed: %s", exc)
        return MarketAnalysis(summary=f"Analysis unavailable: {exc}", regime_assessment="unknown"), f"LLM call failed: {exc}"

    analysis = _parse_market_analysis(response_text, ctx)
    reasoning = f"Financial CoT market analysis (focus={focus}):\n\n{response_text}"
    return analysis, reasoning


def _parse_market_analysis(text: str, ctx: ResearchContext) -> MarketAnalysis:
    """Parse market analysis from LLM response."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return MarketAnalysis(summary=text[:500], regime_assessment=ctx.regime_label)

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return MarketAnalysis(summary=text[:500], regime_assessment=ctx.regime_label)

    return MarketAnalysis(
        summary=str(data.get("summary", "")),
        regime_assessment=str(data.get("regime_assessment", ctx.regime_label)),
        risks=data.get("risks") or [],
        opportunities=data.get("opportunities") or [],
        factor_implications=data.get("factor_implications") or {},
        confidence=_safe_confidence(data.get("confidence", 0.5)),
    )


async def evaluate_regime_transition(
    db: Session,
    user_id: str,
    from_regime: str,
    to_regime: str,
    force_local: bool = False,
) -> tuple[RegimeTransitionEval, str]:
    """Evaluate the plausibility and implications of a regime transition.

    Args:
        db: Database session
        user_id: User ID
        from_regime: Current regime label
        to_regime: Proposed new regime label

    Returns:
        (evaluation, reasoning_trace)
    """
    ctx = _build_research_context(db, user_id)

    prompt = f"""You are a quantitative macro strategist evaluating a potential regime transition.

## Current State
{ctx.to_prompt_context()}

## Transition Under Evaluation
From: {from_regime}
To: {to_regime}

## Analysis Requirements
1. **Plausibility** — How likely is this transition given current macro conditions?
2. **Leading indicators** — What signals would confirm this transition?
3. **Portfolio implications** — How should factor exposures change?
4. **Risk management** — What hedging or rebalancing actions are warranted?
5. **Timeline** — Is this imminent or a gradual shift?

## Output Format
Respond as a JSON object with keys:
- plausibility: One of "low", "medium", "high"
- implications: 2-3 sentence summary of what this means for portfolios
- recommended_actions: Array of specific action strings
- factor_adjustments: Object mapping factor names to adjustment (-1.0 to +1.0)
- reasoning: Your step-by-step reasoning

Only output the JSON object, no other text.
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        completion = await llm_call(
            db,
            task_type="batch_research",
            messages=messages,
            force_local=force_local,
            timeout_s=90.0,
        )
        response_text = completion.content
    except Exception as exc:
        logger.warning("LLM regime transition eval failed: %s", exc)
        return RegimeTransitionEval(
            from_regime=from_regime,
            to_regime=to_regime,
            plausibility="low",
            implications=f"Evaluation unavailable: {exc}",
        ), f"LLM call failed: {exc}"

    evaluation = _parse_regime_transition(response_text, from_regime, to_regime)
    reasoning = f"Financial CoT regime transition ({from_regime} -> {to_regime}):\n\n{response_text}"
    return evaluation, reasoning


def _parse_regime_transition(text: str, from_regime: str, to_regime: str) -> RegimeTransitionEval:
    """Parse regime transition evaluation from LLM response."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return RegimeTransitionEval(
            from_regime=from_regime,
            to_regime=to_regime,
            plausibility="low",
            implications=text[:500],
        )

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return RegimeTransitionEval(
            from_regime=from_regime,
            to_regime=to_regime,
            plausibility="low",
            implications=text[:500],
        )

    return RegimeTransitionEval(
        from_regime=from_regime,
        to_regime=to_regime,
        plausibility=str(data.get("plausibility", "low")),
        implications=str(data.get("implications", "")),
        recommended_actions=data.get("recommended_actions") or [],
        factor_adjustments=data.get("factor_adjustments") or {},
    )


def get_research_summary(db: Session, user_id: str) -> dict[str, Any]:
    """Return a summary of recent LLM research activity."""
    # Count recent factor proposals (stored as QuantSignal entries)
    from app.foundation.models.entities import QuantSignal
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(days=30)
    signals = db.query(QuantSignal).filter(
        QuantSignal.as_of_date >= cutoff.date(),
    ).all()

    return {
        "recent_proposals": len(signals),
        "signals": [
            {
                "id": s.id,
                "name": s.signal_name,
                "direction": s.signal_value,
                "as_of_date": s.as_of_date.isoformat() if s.as_of_date else None,
            }
            for s in signals[:10]
        ],
    }
