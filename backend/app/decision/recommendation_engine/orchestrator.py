"""Orchestrator for the Investment Recommendation Engine.

Coordinates the full pipeline:
1. Build context from all services
2. Generate LLM prompt with available data inventory
3. Call LLM with constrained prompt
4. Parse and validate LLM response
5. Persist report to database
6. Return validated report
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy.orm import sessionmaker

from app.decision.recommendation_engine.context_builder import build_context_bundle
from app.decision.recommendation_engine.models import (
    ContextBundle,
    RecommendationItem,
    RecommendationReport,
    UserProfile,
)
from app.decision.recommendation_engine.prompts import build_recommendation_prompt
from app.decision.recommendation_engine.validator import validate_report

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class GenerateRecommendationsResult(TypedDict):
    report: RecommendationReport
    persisted: bool
    context: ContextBundle


async def generate_recommendations(
    db: Session,
    user_id: str,
    *,
    user_profile: UserProfile | None = None,
    weights: dict[str, float] | None = None,
    candidate_tickers: list[str] | None = None,
) -> GenerateRecommendationsResult:
    """Generate investment recommendations for a user.

    This is the main entry point for the recommendation engine.

    Args:
        db: Database session
        user_id: User requesting recommendations
        user_profile: Optional user risk profile
        weights: Optional custom category weights

    Returns:
        Dict with keys:
        - "report": RecommendationReport with validated recommendations
        - "persisted": bool indicating whether the report was successfully persisted to DB
    """
    report_id = str(uuid.uuid4())

    # Step 1: Build context
    logger.info("Building context for user %s", user_id)
    # build_context_bundle does blocking market-data I/O (per-ticker history
    # across the provider chain). Offload it to a worker thread with its own
    # session so it doesn't stall the event loop for every other request.
    def _build_ctx(bind):
        session = sessionmaker(bind=bind, autoflush=False, autocommit=False)()
        try:
            return build_context_bundle(
                session, user_id, user_profile=user_profile, candidate_tickers=candidate_tickers
            )
        finally:
            session.close()

    context = await asyncio.to_thread(_build_ctx, db.get_bind())

    # Step 2: Check data health
    if context.data_health.completeness_score < 0.1:
        logger.warning(
            "Insufficient data for recommendations (completeness: %.2f)",
            context.data_health.completeness_score,
        )
        report = _build_insufficient_data_report(context, user_id, report_id, weights)
        return {"report": report, "persisted": False, "context": context}

    # Step 3: Generate prompt
    prompt = build_recommendation_prompt(context, weights=weights)

    # Step 4: Call LLM
    logger.info("Calling LLM for recommendations (report %s)", report_id)
    try:
        raw_response = await _call_llm(db, prompt, user_id)
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        report = _build_error_report(context, user_id, report_id, str(e), weights)
        return {"report": report, "persisted": False, "context": context}

    # Step 5: Parse response
    items = _parse_llm_response(raw_response)

    # Step 6: Build report
    report = RecommendationReport(
        report_id=report_id,
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        portfolio_value=context.portfolio.total_value,
        currency=context.portfolio.currency,
        regime_label=context.regime.label,
        regime_confidence=context.regime.confidence,
        recommendations=items,
        source_health=context.data_health,
        weights=weights or {
            "quant_metrics": 0.25,
            "fundamentals": 0.25,
            "sentiment": 0.15,
            "regime": 0.20,
            "news": 0.15,
        },
        raw_llm_output=raw_response,
    )

    # Step 7: Validate
    validated_report, stripped = validate_report(report, context)
    if stripped:
        logger.info("Stripped %d unverifiable claims", len(stripped))

    # Step 8: Persist
    persisted = False
    try:
        _persist_report(db, validated_report)
        logger.info("Persisted report %s", report_id)
        persisted = True
    except Exception as e:
        logger.error("Failed to persist report: %s", e)

    return {"report": validated_report, "persisted": persisted, "context": context}


async def _call_llm(db: Session, prompt: str, user_id: str) -> str:
    """Call the LLM router with the recommendation prompt."""
    from app.foundation.llm.router import call as llm_call

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Generate recommendations based on the provided data."},
    ]

    completion = await llm_call(
        db,
        task_type="routine",
        messages=messages,
        timeout_s=120.0,
        user_id=user_id,
    )

    return completion.content


def _parse_llm_response(raw: str) -> list[RecommendationItem]:
    """Parse LLM JSON response into RecommendationItem objects.

    Handles common LLM formatting issues:
    - Markdown code blocks around JSON
    - Trailing commas
    - Partial responses
    """
    text = raw.strip()

    # Strip markdown code blocks
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (```json and ```)
        lines = [ln for ln in lines[1:] if not ln.strip().startswith("```")]
        text = "\n".join(lines)

    # Try to parse as JSON array
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array in the text
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                logger.error("Failed to parse LLM response as JSON")
                return []
        else:
            logger.error("No JSON array found in LLM response")
            return []

    if not isinstance(data, list):
        data = [data]

    items = []
    for d in data:
        if not isinstance(d, dict):
            continue
        try:
            # Extract evidence
            evidence_data = d.get("evidence", [])
            evidence = []
            for ev in evidence_data:
                if isinstance(ev, dict):
                    evidence.append({
                        "source": ev.get("source", ""),
                        "value": ev.get("value"),
                        "interpretation": ev.get("interpretation", ""),
                    })

            horizon_months = d.get("horizon_months")
            try:
                horizon_months = int(horizon_months) if horizon_months is not None else None
            except (TypeError, ValueError):
                horizon_months = None

            item = RecommendationItem(
                ticker=d.get("ticker", "UNKNOWN"),
                action=d.get("action", "HOLD"),
                confidence=d.get("confidence", "low"),
                timeframe=d.get("timeframe", "3-6 months"),
                thesis=d.get("thesis", ""),
                bull_case=d.get("bull_case", ""),
                bear_case=d.get("bear_case", ""),
                horizon_months=horizon_months,
                evidence=evidence,
                risks=d.get("risks", []),
                data_quality=d.get("data_quality", "complete"),
            )
            items.append(item)
        except Exception as e:
            logger.warning("Failed to parse recommendation item: %s", e)
            continue

    return items


def _persist_report(db: Session, report: RecommendationReport) -> None:
    """Persist the report and its items to the database."""
    from sqlalchemy import text

    # Insert report
    report_sql = text("""
        INSERT INTO recommendation_reports (
            id, user_id, generated_at, portfolio_value, currency,
            regime_label, regime_confidence, estimate, not_tax_advice,
            weights_json, source_health_json, raw_llm_output,
            validated_output, stripped_claims_json
        ) VALUES (
            :id, :user_id, :generated_at, :portfolio_value, :currency,
            :regime_label, :regime_confidence, :estimate, :not_tax_advice,
            :weights_json, :source_health_json, :raw_llm_output,
            :validated_output, :stripped_claims_json
        )
    """)

    db.execute(report_sql, {
        "id": report.report_id,
        "user_id": report.user_id,
        "generated_at": report.generated_at,
        "portfolio_value": report.portfolio_value,
        "currency": report.currency,
        "regime_label": report.regime_label,
        "regime_confidence": report.regime_confidence,
        "estimate": report.estimate,
        "not_tax_advice": report.not_tax_advice,
        "weights_json": json.dumps(report.weights),
        "source_health_json": json.dumps({
            "available": report.source_health.available,
            "failed": report.source_health.failed,
            "degraded": report.source_health.degraded,
            "completeness_score": report.source_health.completeness_score,
        }),
        "raw_llm_output": report.raw_llm_output,
        "validated_output": report.validated_output,
        "stripped_claims_json": json.dumps(report.stripped_claims),
    })

    # Insert items
    item_sql = text("""
        INSERT INTO recommendation_items (
            id, report_id, ticker, action, confidence, timeframe,
            thesis, evidence_json, risks_json, data_quality
        ) VALUES (
            :id, :report_id, :ticker, :action, :confidence, :timeframe,
            :thesis, :evidence_json, :risks_json, :data_quality
        )
    """)

    for item in report.recommendations:
        db.execute(item_sql, {
            "id": str(uuid.uuid4()),
            "report_id": report.report_id,
            "ticker": item.ticker,
            "action": item.action,
            "confidence": item.confidence,
            "timeframe": item.timeframe,
            "thesis": item.thesis,
            "evidence_json": json.dumps([e.model_dump() for e in item.evidence]),
            "risks_json": json.dumps(item.risks),
            "data_quality": item.data_quality,
        })

    db.commit()


def _build_insufficient_data_report(
    context: ContextBundle,
    user_id: str,
    report_id: str,
    weights: dict[str, float] | None,
) -> RecommendationReport:
    """Build a report indicating insufficient data."""
    return RecommendationReport(
        report_id=report_id,
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        portfolio_value=context.portfolio.total_value,
        currency=context.portfolio.currency,
        regime_label=context.regime.label,
        regime_confidence=context.regime.confidence,
        recommendations=[],
        source_health=context.data_health,
        weights=weights or {},
        raw_llm_output="",
        validated_output="",
        stripped_claims=["Insufficient data for recommendations"],
    )


def _build_error_report(
    context: ContextBundle,
    user_id: str,
    report_id: str,
    error: str,
    weights: dict[str, float] | None,
) -> RecommendationReport:
    """Build a report indicating an error occurred.
    
    Sanitizes sensitive error details for API-facing fields while logging
    the full error internally for debugging.
    """
    # Log the full error for debugging
    logger.debug("Full error details: %s", error)

    # Use a safe generic error message for API
    safe_error_msg = "An internal error occurred while generating recommendations"

    return RecommendationReport(
        report_id=report_id,
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        portfolio_value=context.portfolio.total_value,
        currency=context.portfolio.currency,
        regime_label=context.regime.label,
        regime_confidence=context.regime.confidence,
        recommendations=[],
        source_health=context.data_health,
        weights=weights or {},
        raw_llm_output=safe_error_msg,
        validated_output="",
        stripped_claims=[safe_error_msg],
    )
