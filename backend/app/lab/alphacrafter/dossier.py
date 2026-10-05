"""Recommendation dossier builder for AlphaCrafter (Phase 4).

Packages the Miner / Screener / Trader outputs into a research-grade dossier:
position size, rationale, risk notes, and conviction. Conviction is derived
from the real backtest Sharpe, the supporting factors' mean IC, and the
regime fit (with a haircut in a crisis), rather than a hand-set constant.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.foundation.models.entities import RecommendationDossier
from app.lab.alphacrafter.shared_memory import SharedMemoryH

logger = logging.getLogger(__name__)


@dataclass
class DossierContent:
    """Content of a recommendation dossier."""

    symbol: str
    conviction: float
    verdict: str  # buy / hold / review — derived from conviction thresholds
    size: str
    rationale: str
    risk_notes: str
    miner_factors: list[str]
    screener_regime: str | None
    trader_config: dict[str, Any]
    # Surfaced at the top level for the dossier detail view.
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    mean_ic: float = 0.0
    regime_score: float | None = None
    crisis: bool = False
    linked_attribution_run_id: UUID | None = None
    linked_mc_run_id: UUID | None = None


MAX_CONVICTION = 0.95
VERDICT_BUY_THRESHOLD = 0.6
VERDICT_REVIEW_THRESHOLD = 0.3


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_verdict(conviction: float) -> str:
    """Map a conviction score to a buy/hold/review verdict."""
    if conviction >= VERDICT_BUY_THRESHOLD:
        return "buy"
    if conviction >= VERDICT_REVIEW_THRESHOLD:
        return "hold"
    return "review"


def compute_conviction(
    *,
    sharpe: float,
    mean_ic: float,
    regime_score: float | None = None,
    crisis: bool = False,
    sortino: float | None = None,
    cvar: float | None = None,
) -> float:
    """Blend backtest Sharpe, factor IC, regime fit, Sortino, and CVaR into a 0–0.95 conviction.

    * Sharpe is normalised so a Sharpe of 3 maps to a full score.
    * |mean IC| of 0.10 maps to a full score.
    * Regime fit (the Screener's mean selected score), when available, maps a
      score of 0.15 to full and contributes 20% of the blend.
    * Sortino and CVaR are optional enhancements when available.
    * A crisis applies a 30% haircut — we keep some skin in the game but de-risk.
    """
    s = _clip01(sharpe / 3.0)
    i = _clip01(abs(mean_ic) / 0.10)

    # Extended weights when Sortino + CVaR available
    sortino_component = 0.0
    cvar_component = 0.0
    if sortino is not None:
        sortino_component = _clip01(sortino / 3.0)
    if cvar is not None:
        cvar_component = _clip01(1.0 - abs(cvar))  # lower CVaR → higher score

    if regime_score is not None:
        r = _clip01(regime_score / 0.15)
        if sortino is not None and cvar is not None:
            base = 0.30 * s + 0.25 * i + 0.15 * r + 0.15 * sortino_component + 0.15 * cvar_component
        else:
            base = 0.45 * s + 0.35 * i + 0.20 * r
    else:
        if sortino is not None and cvar is not None:
            base = 0.35 * s + 0.30 * i + 0.15 * sortino_component + 0.20 * cvar_component
        else:
            base = 0.6 * s + 0.4 * i
    if crisis:
        base *= 0.7
    return round(min(_clip01(base), MAX_CONVICTION), 4)


async def build_dossier(
    db: Session,
    *,
    symbol: str,
    miner_factors: list[str],
    screener_regime: str | None,
    trader_config: dict[str, Any],
    mean_ic: float = 0.0,
    regime_score: float | None = None,
    crisis: bool = False,
    conviction: float | None = None,
    size_pct: float | None = None,
    attribution_run_id: UUID | None = None,
    mc_run_id: UUID | None = None,
    user_id: str | None = None,
) -> RecommendationDossier:
    """Build and persist a recommendation dossier from AlphaCrafter outputs.

    Args:
        db: Database session.
        symbol: Symbol being recommended.
        miner_factors: Names of the factors supporting the recommendation.
        screener_regime: Current regime label.
        trader_config: Winning trader config + metrics (sharpe_ratio, max_drawdown,
            total_return, rebalance_freq, ...).
        mean_ic: Mean IC across the supporting factors (drives conviction).
        regime_score: Screener's mean selected score for the current regime.
        crisis: Whether the crisis gate is set (haircuts conviction).
        conviction: Override; if ``None`` it is computed from the inputs above.
        size_pct: Suggested position size %; if ``None`` it scales with conviction.
        attribution_run_id / mc_run_id: Optional links to risk artifacts.

    Returns:
        The persisted :class:`RecommendationDossier`.
    """
    sharpe = float(trader_config.get("sharpe_ratio", trader_config.get("sharpe", 0.0)) or 0.0)
    max_dd = float(trader_config.get("max_drawdown", trader_config.get("max_dd", 0.0)) or 0.0)

    if conviction is None:
        conviction = compute_conviction(
            sharpe=sharpe, mean_ic=mean_ic, regime_score=regime_score, crisis=crisis
        )
    if size_pct is None:
        # Scale size with conviction, capped at 10% of the portfolio.
        size_pct = round(conviction * 10.0, 1)

    rationale = (
        f"AlphaCrafter recommends {symbol} on {len(miner_factors)} supporting "
        f"factor(s): {', '.join(miner_factors) or 'none'} (mean IC {mean_ic:.3f})."
    )
    if screener_regime:
        rationale += f" Regime: {screener_regime}."

    risk_notes = (
        f"Backtest Sharpe {sharpe:.2f}, max drawdown {max_dd:.2%}, "
        f"rebalance {trader_config.get('rebalance_freq', 'D')}."
    )
    if crisis:
        risk_notes += " Crisis gate active — conviction haircut applied."
    if mc_run_id:
        risk_notes += " See linked Monte Carlo for forward-looking risk."

    content = DossierContent(
        symbol=symbol,
        conviction=conviction,
        verdict=compute_verdict(conviction),
        size=f"{size_pct:.1f}%",
        rationale=rationale,
        risk_notes=risk_notes,
        miner_factors=miner_factors,
        screener_regime=screener_regime,
        trader_config=trader_config,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        mean_ic=mean_ic,
        regime_score=regime_score,
        crisis=crisis,
        linked_attribution_run_id=attribution_run_id,
        linked_mc_run_id=mc_run_id,
    )

    dossier = RecommendationDossier(
        ts=datetime.now(UTC),
        user_id=user_id,
        conviction=conviction,
        dossier_json=json.dumps(asdict(content), default=str),
        attribution_run_id=str(attribution_run_id) if attribution_run_id else None,
        mc_run_id=str(mc_run_id) if mc_run_id else None,
    )

    db.add(dossier)
    db.commit()
    return dossier


class DossierBuilder:
    """Agent that packages pipeline outputs into recommendation dossiers.

    Reads from SharedMemoryH (trader_outputs, screener_outputs, factor_states)
    and writes evaluation results and agent history back to H.
    """

    async def build(self, h: SharedMemoryH, db: Session, user_id: str | None = None) -> SharedMemoryH:
        """Build recommendation dossiers from H and persist them.

        Args:
            h: SharedMemoryH containing pipeline outputs.
            db: Database session for persisting RecommendationDossier rows.
            user_id: Owner of the created dossiers (user-scoped reads).

        Returns:
            Updated SharedMemoryH with evaluation results and agent history.
        """
        trader_outputs = h.trader_outputs
        screener_outputs = h.screener_outputs
        factor_states = h.factor_states

        targets = h.market_state.universe
        if not targets:
            h.evaluation = {"dossiers_created": 0, "conviction_scores": {}}
            h.append_history("dossier", "build", {"dossiers_created": 0})
            return h

        configs = trader_outputs.get("configs", [])
        if not configs:
            h.evaluation = {"dossiers_created": 0, "conviction_scores": {}}
            h.append_history("dossier", "build", {"dossiers_created": 0})
            return h

        best_config = max(configs, key=lambda c: c.get("sharpe_ratio", 0))
        sharpe = best_config.get("sharpe_ratio", 0.0)

        regime_score = screener_outputs.get("regime_score")
        regime_label = screener_outputs.get("regime_label") or h.market_state.regime_label
        crisis = screener_outputs.get("crisis", False) or h.market_state.crisis

        factor_names = [fs.name for fs in factor_states]

        if factor_states:
            mean_ic = sum(fs.metrics.ic for fs in factor_states) / len(factor_states)
        else:
            mean_ic = 0.0

        conviction = compute_conviction(
            sharpe=sharpe,
            mean_ic=mean_ic,
            regime_score=regime_score,
            crisis=crisis,
        )

        conviction_scores = {}
        dossiers_created = 0

        for symbol in targets:
            await build_dossier(
                db,
                symbol=symbol,
                miner_factors=factor_names,
                screener_regime=regime_label,
                trader_config=best_config,
                mean_ic=mean_ic,
                regime_score=regime_score,
                crisis=crisis,
                conviction=conviction,
                user_id=user_id,
            )
            conviction_scores[symbol] = conviction
            dossiers_created += 1

        h.evaluation = {
            "dossiers_created": dossiers_created,
            "conviction_scores": conviction_scores,
            "regime_label": regime_label,
            "factor_names": factor_names,
            "best_sharpe": sharpe,
            "mean_ic": mean_ic,
        }

        h.append_history(
            "dossier",
            "build",
            {
                "dossiers_created": dossiers_created,
                "conviction_scores": conviction_scores,
                "regime_label": regime_label,
            },
        )

        return h
