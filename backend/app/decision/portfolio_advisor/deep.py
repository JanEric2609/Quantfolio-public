"""Deep mode — LLM-powered portfolio analysis with regime context."""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEEP_SYSTEM_PROMPT = """You are a professional portfolio advisor assistant.
Analyze the portfolio data provided and produce a concise assessment.
Focus on: diversification, risk exposure, regime alignment, and specific rebalancing suggestions.
Keep your response to 3-5 paragraphs. Be specific with numbers and percentages."""


def build_deep_prompt(
    holdings: list[dict],
    regime: dict | None,
    pulse_result: dict | None = None,
    regime_weights: dict | None = None,
) -> str:
    """Build a detailed prompt for LLM analysis."""
    parts = ["## Portfolio Holdings"]
    for h in holdings:
        name = h.get("name", "Unknown")
        val = h.get("current_value", 0) or 0
        atype = h.get("asset_type", "other")
        parts.append(f"- {name}: EUR {val:,.0f} ({atype})")

    if regime:
        parts.append("\n## Current Regime")
        parts.append(f"Label: {regime.get('label', 'unknown')}")
        parts.append(f"Confidence: {regime.get('confidence', 0):.0%}")
        parts.append(f"VIX: {regime.get('vix')}")
        parts.append(f"Yield Spread: {regime.get('yield_spread')}")
        parts.append(f"Momentum (3M): {regime.get('momentum_3m')}")

    if pulse_result:
        parts.append(f"\n## Pulse Check Results (Status: {pulse_result.get('status')})")
        for c in pulse_result.get("checks", []):
            parts.append(f"- [{c.get('severity')}] {c.get('message')}")

    # Factor gap analysis vs MSCI World baseline
    from app.foundation.gap_analysis import compute_factor_gaps
    ticker_weights = _build_ticker_weights(holdings)
    factor_gaps = compute_factor_gaps(ticker_weights)
    if factor_gaps:
        parts.append("\n## Factor Exposure Gaps vs MSCI World")
        for g in factor_gaps:
            parts.append(
                f"- {g['factor']}: {g['current']*100:.0f}% actual vs {g['target']*100:.0f}% target "
                f"({g['direction']}, gap={g['gap']*100:+.0f}pp)"
            )

    if regime_weights:
        parts.append("\n## Regime-Adjusted Weights (MWU)")
        parts.append("Historical accuracy-based weights by strategy action:")
        for action, weight in sorted(regime_weights.items()):
            pct = weight * 100
            marker = "★" if weight > 0.25 else "·"
            parts.append(f"- {marker} {action}: {pct:.0f}% confidence")
        top_action = max(regime_weights, key=lambda k: regime_weights.get(k, 0))  # type: ignore[arg-type]
        parts.append(f"→ Highest-weighted action: **{top_action}** — this strategy has performed best in the current regime historically.")

    total_value = sum((h.get("current_value", 0) or 0) for h in holdings)
    parts.append("\n## Instructions")
    parts.append(f"Total portfolio value: EUR {total_value:,.0f}")
    parts.append("Please provide:")
    parts.append("1. Overall portfolio health assessment")
    parts.append("2. Key risks identified")
    parts.append("3. Specific rebalancing suggestions (with percentages)")
    parts.append("4. Regime alignment analysis")
    return "\n".join(parts)


def _build_ticker_weights(holdings: list[dict]) -> list[dict]:
    """Convert holdings list into weight-normalised ticker list for gap analysis."""
    total = sum((h.get("current_value", 0) or 0) for h in holdings) or 1
    result = []
    for h in holdings:
        ticker = h.get("ticker") or ""
        if ticker:
            result.append({"ticker": ticker, "weight": (h.get("current_value", 0) or 0) / total})
    return result


def make_llm_func(db: Session) -> Callable[[str, str], str]:
    """Return a synchronous LLM callable for use in background tasks and sync handlers."""
    import asyncio
    from app.foundation.llm.router import call as llm_call

    def _llm(system_prompt: str, user_prompt: str) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        completion = asyncio.run(llm_call(db, "routine", messages))
        return completion.content

    return _llm


def run_deep_analysis(holdings: list[dict], regime: dict | None, llm_func=None, regime_weights: dict | None = None, lookthrough: dict | None = None) -> dict:
    """Run LLM-powered deep analysis with regime-adjusted weights."""
    from .pulse import run_pulse_check
    from app.foundation.gap_analysis import compute_factor_gaps
    pulse = run_pulse_check(holdings, regime, regime_weights, lookthrough)
    factor_gaps = compute_factor_gaps(_build_ticker_weights(holdings))
    prompt = build_deep_prompt(holdings, regime, pulse, regime_weights)

    if llm_func is None:
        # Fallback: structured analysis without LLM
        return _fallback_analysis(holdings, regime, pulse, factor_gaps)

    try:
        llm_response = llm_func(DEEP_SYSTEM_PROMPT, prompt)
        return {
            "mode": "deep",
            "llm_analysis": llm_response,
            "pulse": pulse,
            "factor_gaps": factor_gaps,
            "regime": regime,
        }
    except Exception as e:
        logger.warning("LLM deep analysis failed: %s", e)
        return _fallback_analysis(holdings, regime, pulse, factor_gaps)


def _fallback_analysis(holdings: list[dict], regime: dict | None, pulse: dict, factor_gaps: list[dict] | None = None) -> dict:
    """Structured analysis when LLM is unavailable."""
    total = sum((h.get("current_value", 0) or 0) for h in holdings) or 1
    by_type: dict[str, float] = {}
    for h in holdings:
        at = h.get("asset_type", "other")
        by_type[at] = by_type.get(at, 0) + (h.get("current_value", 0) or 0)
    allocation_pct = {k: round(v / total * 100, 1) for k, v in by_type.items()}
    top_holdings = sorted(holdings, key=lambda h: h.get("current_value", 0) or 0, reverse=True)[:5]

    return {
        "mode": "deep_fallback",
        "pulse": pulse,
        "factor_gaps": factor_gaps or [],
        "regime": regime,
        "analysis": {
            "total_value": total,
            "allocation": allocation_pct,
            "holding_count": len(holdings),
            "top_holdings": [
                {"name": h.get("name"), "value": h.get("current_value"), "pct": round((h.get("current_value", 0) or 0) / total * 100, 1)}
                for h in top_holdings
            ],
            "note": "LLM unavailable — showing quantitative overview",
        },
    }
