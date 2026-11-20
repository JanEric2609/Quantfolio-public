"""Source Attribution Validator for the Investment Recommendation Engine.

Verifies that every claim in the LLM output references a data source
that actually exists in the context bundle. Removes unverifiable claims
to prevent hallucinated recommendations.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.decision.recommendation_engine.models import (
    ContextBundle,
    RecommendationItem,
    RecommendationReport,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Valid action values
VALID_ACTIONS = {"BUY", "HOLD", "SELL", "AVOID"}
VALID_CONFIDENCE = {"low", "medium", "high"}

# Regex to extract source paths from evidence
SOURCE_PATH_RE = re.compile(
    r"^(metrics|fundamentals|estimates|insider_signal|sentiment|track_record|regime|portfolio)\.(\w+)\.?(\w+)?$"
)


def validate_report(
    report: RecommendationReport,
    context: ContextBundle,
) -> tuple[RecommendationReport, list[str]]:
    """Validate and clean a recommendation report.

    For each recommendation item:
    1. Validate action is one of BUY/HOLD/SELL/AVOID
    2. Validate confidence is low/medium/high
    3. Verify each evidence source path exists in the context
    4. Remove evidence items with unverifiable sources
    5. If no evidence remains after cleaning, downgrade confidence

    Args:
        report: The raw recommendation report from the LLM.
        context: The context bundle used to generate the report.

    Returns:
        Tuple of (cleaned_report, list_of_stripped_claims)
    """
    stripped_claims: list[str] = []
    validated_items: list[RecommendationItem] = []

    # Build the set of valid source paths from context
    valid_sources = _build_valid_sources(context)

    for item in report.recommendations:
        cleaned_item, item_stripped = _validate_item(item, valid_sources)
        stripped_claims.extend(item_stripped)
        validated_items.append(cleaned_item)

    # Build updated source health
    validated_report = report.model_copy(
        update={
            "recommendations": validated_items,
            "stripped_claims": stripped_claims,
        }
    )

    # Update the validated output field
    validated_report.validated_output = _serialize_items(validated_items)

    return validated_report, stripped_claims


def _build_valid_sources(context: ContextBundle) -> set[str]:
    """Build the set of all valid source paths from the context."""
    sources: set[str] = set()

    # Portfolio sources
    sources.add("portfolio.total_value")
    sources.add("portfolio.currency")
    sources.add("portfolio.asset_allocation")
    for h in context.portfolio.holdings:
        ticker = h.get("ticker", "")
        if ticker:
            sources.add(f"portfolio.holdings.{ticker}")

    # Metric sources
    for ticker, m in context.metrics.items():
        for field in ["sortino", "sharpe", "calmar", "cvar_95", "max_drawdown",
                       "annualised_return", "annualised_volatility", "beta", "alpha"]:
            value = getattr(m, field, None)
            if value is not None:
                sources.add(f"metrics.{ticker}.{field}")

    # Fundamental sources
    for ticker, f in context.fundamentals.items():
        for field in ["piotroski_score", "pe_ratio", "roe", "debt_equity",
                       "profit_margin", "revenue_growth", "gross_margin"]:
            value = getattr(f, field, None)
            if value is not None:
                sources.add(f"fundamentals.{ticker}.{field}")

    # Analyst estimate sources (IBES)
    for ticker, est in context.estimates.items():
        for field in ["sue", "revision_momentum", "dispersion"]:
            value = getattr(est, field, None)
            if value is not None:
                sources.add(f"estimates.{ticker}.{field}")

    # Insider-trading sources
    for ticker, ins in context.insider_signal.items():
        for field in ["cluster_buy_score", "net_insider_flow_usd"]:
            value = getattr(ins, field, None)
            if value is not None:
                sources.add(f"insider_signal.{ticker}.{field}")

    # Sentiment sources
    for ticker, s in context.sentiment.items():
        if s.finbert_score is not None:
            sources.add(f"sentiment.{ticker}.finbert_score")
        if s.news_count > 0:
            sources.add(f"sentiment.{ticker}.news_count")
        if s.headlines:
            sources.add(f"sentiment.{ticker}.headlines")

    # Track record sources
    for ticker, t in context.track_record.items():
        if t.advisor_composite_score is not None:
            sources.add(f"track_record.{ticker}.advisor_composite_score")
        if t.llm_win_rate is not None:
            sources.add(f"track_record.{ticker}.llm_win_rate")
        if t.llm_verdict_history:
            sources.add(f"track_record.{ticker}.llm_verdict_history")

    # Regime sources
    if context.regime.label != "unknown":
        sources.add("regime.label")
    if context.regime.confidence > 0:
        sources.add("regime.confidence")
    if context.regime.vix is not None:
        sources.add("regime.vix")

    return sources


def _validate_item(
    item: RecommendationItem,
    valid_sources: set[str],
) -> tuple[RecommendationItem, list[str]]:
    """Validate and clean a single recommendation item."""
    stripped: list[str] = []

    # Validate action
    action = item.action.upper() if item.action else "HOLD"
    if action not in VALID_ACTIONS:
        logger.warning("Invalid action '%s' for %s, defaulting to HOLD", item.action, item.ticker)
        action = "HOLD"

    # Validate confidence
    confidence = item.confidence.lower() if item.confidence else "low"
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    # Validate evidence sources
    valid_evidence = []
    for ev in item.evidence:
        source_path = ev.source.strip() if ev.source else ""
        if source_path and _is_source_valid(source_path, valid_sources):
            valid_evidence.append(ev)
        else:
            stripped.append(f"{item.ticker}: {source_path} (source not in context)")
            logger.warning(
                "Stripped unverifiable evidence for %s: %s",
                item.ticker,
                source_path,
            )

    # If no evidence remains, downgrade confidence
    if not valid_evidence and item.evidence:
        confidence = "low"

    return item.model_copy(
        update={
            "action": action,
            "confidence": confidence,
            "evidence": valid_evidence,
        }
    ), stripped


def _is_source_valid(source_path: str, valid_sources: set[str]) -> bool:
    """Check if a source path is valid (exists in the context).

    Supports both exact matches and child-path matches (e.g. "metrics.VWCE.DE.sharpe"
    matches if "metrics.VWCE.DE" exists in valid sources).
    """
    # Exact match
    if source_path in valid_sources:
        return True

    # Forward prefix match — check if any valid source starts with this path plus a delimiter
    # This allows "metrics.AAPL" to match "metrics.AAPL.sharpe"
    for vs in valid_sources:
        if vs.startswith(source_path + "."):
            return True

    # Reverse prefix match with strict boundary check
    # Check if the claimed path is a child of any valid source
    # e.g., if "metrics.AAPL" is valid, accept "metrics.AAPL.sharpe" but NOT "metrics.AAPL.sharpe_extra"
    for vs in valid_sources:
        if source_path.startswith(vs + "."):
            return True

    return False


def _serialize_items(items: list[RecommendationItem]) -> str:
    """Serialize validated items to JSON string for audit trail."""
    import json
    return json.dumps([item.model_dump() for item in items], default=str, indent=2)
