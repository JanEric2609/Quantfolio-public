"""Adapts recommendation_engine's RecommendationItem output to RecommendationPayloadV2.

RecommendationItem (validated, evidence-stripped) doesn't carry every field
RecommendationPayloadV2 requires — there's no numeric scoring, sizing, or role
classification in recommendation_engine's output. Rather than inventing new
heuristics for those, this module reuses alphacrafter.dossier's existing
conviction/sizing formulas (already real, already shipped) and derives the
handful of remaining fields (expected_role, portfolio_fit_score) from simple,
documented rules. See docs/archive/plans/recommendation-discovery-consolidation-implementation.md
Phase 1 item 3 for the full field-by-field rationale.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.foundation.schemas import (
    BacktestSummaryV2,
    RecommendationAsset,
    RecommendationMode,
    RecommendationPayloadV2,
    SuggestedPositionSize,
)
from app.lab.alphacrafter.dossier import compute_conviction
from app.decision.recommendation_engine.models import ContextBundle, RecommendationItem

_CONFIDENCE_BUCKETS = {"low": 25.0, "medium": 55.0, "high": 85.0}
_DATA_QUALITY_BUCKETS = {"complete": 90.0, "partial": 60.0, "insufficient": 20.0}
_DEFAULT_HORIZON_MONTHS = {"short_term": 3, "long_term": 84}

Verdict = Literal[
    "STRONG_CANDIDATE", "CANDIDATE", "WATCH", "HOLD_EXISTING", "AVOID", "REJECTED_BY_RISK", "STALE",
]


def _risk_score(max_drawdown: float | None) -> float:
    """Bucket real drawdown magnitude into a 0-100 score (higher = safer)."""
    if max_drawdown is None:
        return 40.0
    dd = abs(max_drawdown)
    if dd < 0.2:
        return 70.0
    if dd < 0.4:
        return 50.0
    return 30.0


def _portfolio_fit_score(candidate_asset_type: str, context: ContextBundle) -> float:
    """Penalize concentration in an asset class the portfolio is already heavy in."""
    total = context.portfolio.total_value
    if total <= 0:
        return 60.0
    same_class_value = sum(
        h.get("current_value", 0.0)
        for h in context.portfolio.holdings
        if h.get("asset_type") == candidate_asset_type
    )
    weight = same_class_value / total
    if weight >= 0.4:
        return 35.0
    if weight >= 0.2:
        return 55.0
    return 75.0


def _expected_role(
    asset_type: str, size_pct: float
) -> Literal["core", "satellite", "hedge", "cash_like", "income", "speculative"]:
    if asset_type in {"cash", "money_market"}:
        return "cash_like"
    if asset_type in {"bond", "bond_etf"}:
        return "income"
    if asset_type in {"etf", "fund"}:
        return "core"
    return "satellite" if size_pct >= 3.0 else "speculative"


def _verdict_from_conviction(action: str, conviction: float) -> Verdict:
    action = (action or "HOLD").upper()
    if action in {"SELL"}:
        return "REJECTED_BY_RISK"
    if action == "AVOID":
        return "AVOID"
    if action == "HOLD":
        return "HOLD_EXISTING"
    # action == "BUY"
    if conviction >= 0.6:
        return "STRONG_CANDIDATE"
    if conviction >= 0.35:
        return "CANDIDATE"
    if conviction >= 0.15:
        return "WATCH"
    return "AVOID"


def recommendation_item_to_v2(
    item: RecommendationItem,
    *,
    candidate: dict[str, Any],
    context: ContextBundle,
    mode: RecommendationMode,
) -> RecommendationPayloadV2:
    """Build a RecommendationPayloadV2 from a validated RecommendationItem."""
    ticker = item.ticker
    metrics = context.metrics.get(ticker)

    sharpe = metrics.sharpe if metrics and metrics.sharpe is not None else 0.0
    sortino = metrics.sortino if metrics else None
    cvar = metrics.cvar_95 if metrics else None
    max_drawdown = metrics.max_drawdown if metrics else None

    conviction = compute_conviction(
        sharpe=sharpe,
        mean_ic=0.0,
        regime_score=context.regime.confidence if context.regime.label != "unknown" else None,
        crisis=context.regime.label == "crisis",
        sortino=sortino,
        cvar=cvar,
    )

    asset_type = candidate.get("type") or candidate.get("asset_type") or "stock"
    size_pct = round(conviction * 10.0, 1)
    verdict = _verdict_from_conviction(item.action, conviction)

    generated_at = datetime.now(UTC)
    horizon_months = item.horizon_months or _DEFAULT_HORIZON_MONTHS.get(mode, 12)
    expires_at = generated_at + (timedelta(days=21) if mode == "short_term" else timedelta(days=75))

    backtest_summary = BacktestSummaryV2(
        status="ok" if metrics is not None else "unavailable",
        period="most recent 252 trading days",
        sharpe=metrics.sharpe if metrics else None,
        sortino=metrics.sortino if metrics else None,
        max_drawdown=metrics.max_drawdown if metrics else None,
        volatility=metrics.annualised_volatility if metrics else None,
    )

    return RecommendationPayloadV2(
        asset=RecommendationAsset(
            symbol=ticker,
            name=candidate.get("name") or ticker,
            asset_type=asset_type if asset_type in {
                "stock", "etf", "bond", "bond_etf", "fund", "money_market", "cash", "other",
            } else "other",
            currency=candidate.get("currency", "EUR"),
        ),
        mode=mode,
        horizon_months=max(1, min(120, horizon_months)),
        verdict=verdict,
        confidence=_CONFIDENCE_BUCKETS.get(item.confidence, 25.0),
        evidence_score=round(conviction * 100, 1),
        data_quality_score=_DATA_QUALITY_BUCKETS.get(item.data_quality, 20.0),
        risk_score=_risk_score(max_drawdown),
        portfolio_fit_score=_portfolio_fit_score(asset_type, context),
        expected_role=_expected_role(asset_type, size_pct),
        backtest_summary=backtest_summary,
        suggested_position_size=SuggestedPositionSize(
            min_pct=max(0.0, size_pct - 2.0),
            max_pct=min(100.0, size_pct + 2.0),
            reason=(
                f"Sized proportionally to a conviction score of {conviction:.2f} "
                "(blend of backtest Sharpe/Sortino/CVaR and regime fit), capped at 10%."
            ),
        ),
        thesis=item.thesis,
        bull_case=item.bull_case or item.thesis,
        bear_case=item.bear_case or "No specific bear case was provided.",
        risk_notes=list(item.risks),
        why_not_buy=list(item.risks) if verdict in {"AVOID", "REJECTED_BY_RISK", "WATCH"} else [],
        invalidation_triggers=[
            "Reassess if the backtest Sharpe/drawdown materially worsens.",
            "Reassess if the underlying thesis evidence is no longer supported by current data.",
        ],
        required_human_checks=[
            "Verify current price, liquidity, and tradeability before executing.",
        ],
        source_reports=[],
        generated_at=generated_at,
        expires_at=expires_at,
    )
