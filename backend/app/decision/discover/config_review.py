"""Discovery config review engine (P3d).

Evaluates challenger ``DiscoveryConfig`` rows against the current champion,
applies Bonferroni + Deflated-Sharpe-Ratio (DSR) multi-testing correction
with a t-stat threshold of 3, and auto-promotes winners.
"""
from __future__ import annotations

import logging
import math
from typing import Any, cast

import numpy as np
from scipy import stats as scipy_stats
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    DiscoveryConfig,
    DiscoveryConfigReview,
    DiscoveryPrediction,
)
from app.foundation.models.entities._core import now_utc, uuid_pk
from app.decision.discover.composite import (
    compute_weighted_composite,
    derive_signals_from_scores,
    ic_signal_usable,
)
from app.decision.discover.config import (
    activate_config,
    get_active_config,
    get_champion_config,
    list_configs,
)
from app.decision.discover.config_perturb import generate_weight_perturbations

logger = logging.getLogger(__name__)

KNOWN_CONFIG_TYPES = ["signal_weights", "prompt_template"]


def _num(value: Any, default: float) -> float:
    """Coerce *value* to float, falling back to *default*."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_signals(features_json: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    """Reconstruct the signal map used by ``compute_weighted_composite``.

    Supports both the ``signal_breakdown`` shape written by ``store_prediction``
    and the ``scores`` shape written by ``write_predictions``. Delegates to
    :func:`derive_signals_from_scores` so a challenger is scored on the same
    9-signal composite production uses — a review that silently dropped
    momentum/risk/benchmark (50% of production weight) would judge challengers
    on a composite that isn't the one they're actually trying to beat.
    """
    scores: dict[str, Any] = {}
    if isinstance(features_json, dict):
        if "signal_breakdown" in features_json:
            scores = features_json["signal_breakdown"]
        elif "scores" in features_json:
            scores = features_json["scores"]

    raw_miner = scores.get("alpha_miner")
    alpha_miner: dict[str, Any] = raw_miner if isinstance(raw_miner, dict) else {}
    signals = derive_signals_from_scores(scores)
    return signals, alpha_miner


def _score_for_config(
    signals: dict[str, float],
    alpha_miner: dict[str, Any],
    config_json: dict[str, Any],
) -> float:
    """Compute conviction for a single config's weights."""
    cfg = config_json if isinstance(config_json, dict) else {}
    weights = cfg.get("weights") if isinstance(cfg.get("weights"), dict) else None
    threshold = _num(cfg.get("threshold_ic_obs"), 20)
    miner = alpha_miner if isinstance(alpha_miner, dict) else {}
    ic_data = miner if ic_signal_usable(miner, threshold) else None
    return compute_weighted_composite(signals, ic_data=ic_data, weights=weights)


def _rank_ic(convictions: list[float], realised_returns: list[float]) -> float | None:
    """Spearman rank correlation between conviction and realised return."""
    if len(convictions) < 2:
        return None
    try:
        result = scipy_stats.spearmanr(convictions, realised_returns)
        correlation = cast(float, result[0])  # SpearmanrResult is a named tuple, index 0 = correlation
        return None if math.isnan(correlation) else correlation
    except Exception:
        return None


def _brier(convictions: list[float], hits: list[int]) -> float | None:
    """Average Brier score for probabilistic hit predictions."""
    if not convictions:
        return None
    return float(np.mean([(c - h) ** 2 for c, h in zip(convictions, hits)]))


def _sharpe_ratio(realised_returns: list[float]) -> float:
    """t-statistic of per-prediction realised returns (mean/std * sqrt(n)).

    Despite the name, this is not an annualised Sharpe ratio: it's mean/std
    scaled by sqrt(sample size), used downstream as a t-statistic (see
    ``apply_multi_testing_correction``). Delegating to the unified
    ``quant_metrics.sharpe_ratio`` with ``periods_per_year=n`` reproduces
    exactly that scaling, since its annualisation factor is
    ``sqrt(periods_per_year)`` and ``risk_free=0`` means no rf adjustment.
    """
    n = len(realised_returns)
    if n < 2:
        return 0.0
    from app.foundation.quant_metrics import sharpe_ratio

    return sharpe_ratio(realised_returns, risk_free=0.0, periods_per_year=n)


def _top_quartile_hit_rate(convictions: list[float], realised_returns: list[float]) -> float:
    """Hit rate among the top-quartile of convictions."""
    if not convictions:
        return 0.0
    arr_c = np.asarray(convictions, dtype=float)
    arr_r = np.asarray(realised_returns, dtype=float)
    threshold = float(np.percentile(arr_c, 75))
    mask = arr_c >= threshold
    if not np.any(mask):
        return 0.0
    return float(np.mean(arr_r[mask] > 0))


def _expected_calibration_error(
    convictions: list[float],
    realised_returns: list[float],
    n_bins: int = 10,
) -> float:
    """Expected calibration error for binary hit predictions."""
    arr_c = np.asarray(convictions, dtype=float)
    arr_r = np.asarray(realised_returns, dtype=float)
    n = len(arr_c)
    if n == 0:
        return 0.0

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        lo = edges[i]
        hi = edges[i + 1]
        if i == n_bins - 1:
            mask = (arr_c >= lo) & (arr_c <= hi)
        else:
            mask = (arr_c >= lo) & (arr_c < hi)
        count = int(mask.sum())
        if count == 0:
            continue
        avg_conf = float(arr_c[mask].mean())
        avg_out = float((arr_r[mask] > 0).mean())
        ece += (count / n) * abs(avg_conf - avg_out)
    return float(ece)


def collect_reviewable_configs(db: Session) -> tuple[dict[str, DiscoveryConfig], list[DiscoveryConfig]]:
    """Return champions by config_type and all challenger configs.

    A config only reaches ``"champion"`` status by losing an ``activate_config``
    promotion — so on a fresh install (or right after the system-seeded config
    was never promoted against) no champion row exists yet, and review would
    deadlock forever waiting for one. The active config is the de facto
    champion in that case: predictions are tagged with the active config's id
    at write time (``orchestrator.py``), so it already has a resolvable
    prediction history to evaluate against. The first winning promotion demotes
    it to a real ``"champion"`` row via ``activate_config``, after which this
    fallback is no longer needed for that config_type.
    """
    challengers = list_configs(db, status="challenger")
    champions: dict[str, DiscoveryConfig] = {}

    types_to_check = set(KNOWN_CONFIG_TYPES)
    types_to_check.update(c.config_type for c in challengers)

    for config_type in types_to_check:
        champion = get_champion_config(db, config_type) or get_active_config(db, config_type)
        if champion is not None:
            champions[config_type] = champion

    return champions, challengers


def _resolved_predictions_for_champion(db: Session, champion: DiscoveryConfig) -> list[DiscoveryPrediction]:
    """Resolved predictions produced by the champion config."""
    return (
        db.query(DiscoveryPrediction)
        .filter(
            DiscoveryPrediction.config_id == champion.id,
            DiscoveryPrediction.outcome_status == "resolved",
            DiscoveryPrediction.features_json.isnot(None),
            DiscoveryPrediction.realised_return.isnot(None),
        )
        .all()
    )


def evaluate_challengers(
    db: Session,
    champion: DiscoveryConfig,
    challengers: list[DiscoveryConfig],
) -> list[dict[str, Any]]:
    """Score each signal-weight challenger against the champion.

    Prompt-template challengers are forward-only and are skipped (empty list).
    """
    if champion.config_type != "signal_weights":
        return []

    preds = _resolved_predictions_for_champion(db, champion)
    if len(preds) < 30:
        return []

    champion_convs: list[float] = []
    realised_returns: list[float] = []
    hits: list[int] = []

    for pred in preds:
        assert pred.realised_return is not None  # filtered by _resolved_predictions_for_champion
        signals, alpha_miner = _extract_signals(pred.features_json)
        champion_convs.append(_score_for_config(signals, alpha_miner, champion.config_json))
        # Judge against the passive core where the row has it (Phase 2).
        outcome = pred.excess_return if pred.excess_return is not None else pred.realised_return
        realised_returns.append(float(outcome))
        hits.append(1 if outcome > 0 else 0)

    champion_rank_ic = _rank_ic(champion_convs, realised_returns)
    champion_brier = _brier(champion_convs, hits)

    results: list[dict[str, Any]] = []
    for challenger in challengers:
        if challenger.config_type != "signal_weights":
            continue

        challenger_convs: list[float] = []
        for pred in preds:
            signals, alpha_miner = _extract_signals(pred.features_json)
            challenger_convs.append(_score_for_config(signals, alpha_miner, challenger.config_json))

        n = len(challenger_convs)
        if n < 30:
            continue

        per_prediction_ic_deltas = [
            float(c - champ) for c, champ in zip(challenger_convs, champion_convs)
        ]

        results.append(
            {
                "config_id": challenger.id,
                "config_type": challenger.config_type,
                "version_label": challenger.version_label,
                "n_predictions": n,
                "hit_rate": float(np.mean(hits)),
                "brier_avg": _brier(challenger_convs, hits),
                "champion_brier": champion_brier,
                "rank_ic": _rank_ic(challenger_convs, realised_returns),
                "champion_rank_ic": champion_rank_ic,
                "delta_ic": (
                    None
                    if champion_rank_ic is None
                    else float((_rank_ic(challenger_convs, realised_returns) or 0.0) - champion_rank_ic)
                ),
                "top_quartile_hit_rate": _top_quartile_hit_rate(challenger_convs, realised_returns),
                "ece": _expected_calibration_error(challenger_convs, realised_returns),
                "sharpe_ratio": _sharpe_ratio(per_prediction_ic_deltas),
                "per_prediction_ic_deltas": per_prediction_ic_deltas,
            }
        )

    return results


def _correlation_adjusted_sharpe_ratio(
    all_results: list[dict[str, Any]],
    challenger_result: dict[str, Any],
    n_challengers: int,
) -> float:
    """Correlation-adjusted SR: shrinks the per-challenger SR by average pairwise IC-delta correlation.

    Not the canonical Bailey-López de Prado DSR (which is probability-valued).
    Returns an SR-like statistic ≥ 0 that penalises challengers whose IC-delta
    series are highly correlated with other challengers (reduces effective N).
    Gate at t_stat_threshold=3.0 is appropriate for this statistic's range.
    """
    if n_challengers < 2:
        return _num(challenger_result.get("sharpe_ratio"), 0.0)

    deltas = [
        np.asarray(r.get("per_prediction_ic_deltas", []), dtype=float)
        for r in all_results
        if r.get("per_prediction_ic_deltas")
    ]
    if len(deltas) < 2:
        return _num(challenger_result.get("sharpe_ratio"), 0.0)

    corr_sum = 0.0
    corr_count = 0
    for i in range(len(deltas)):
        for j in range(i + 1, len(deltas)):
            if len(deltas[i]) < 2 or len(deltas[j]) < 2:
                continue
            if len(deltas[i]) != len(deltas[j]):
                continue
            corr = float(np.corrcoef(deltas[i], deltas[j])[0, 1])
            if math.isnan(corr):
                continue
            corr_sum += max(0.0, corr)
            corr_count += 1

    gamma = corr_sum / corr_count if corr_count else 0.0
    sr = _num(challenger_result.get("sharpe_ratio"), 0.0)
    denom = 1.0 + (n_challengers - 1) * gamma
    if denom <= 0:
        return 0.0
    return sr * (1.0 - gamma) / denom


def apply_multi_testing_correction(
    results: list[dict[str, Any]],
    alpha: float = 0.05,
    t_stat_threshold: float = 3.0,
) -> dict[str, Any]:
    """Bonferroni + DSR correction; return promotion decision."""
    n = len(results)
    adjusted_alpha = alpha / max(1, n)

    details: list[dict[str, Any]] = []
    promoted: dict[str, Any] | None = None

    for result in results:
        n_pred = int(result.get("n_predictions", 0))
        sr = _num(result.get("sharpe_ratio"), 0.0)

        # sr is mean/std*sqrt(n) from _sharpe_ratio, i.e. already the t-statistic
        if n_pred > 1:
            t_stat = sr
            p_value = float(1.0 - scipy_stats.t.cdf(t_stat, df=n_pred - 1))
        else:
            t_stat = 0.0
            p_value = 1.0

        dsr = _correlation_adjusted_sharpe_ratio(results, result, n)

        brier_avg = _num(result.get("brier_avg"), 1.0)
        champion_brier = _num(result.get("champion_brier"), 1.0)
        brier_delta = brier_avg - champion_brier

        passes = (
            dsr >= t_stat_threshold
            and p_value <= adjusted_alpha  # Bonferroni-corrected significance gate
            and n_pred >= 30
            and brier_delta <= 0.0  # challenger must be at least as well calibrated
        )

        detail = {
            "config_id": result.get("config_id"),
            "n_predictions": n_pred,
            "sharpe_ratio": sr,
            "t_stat": t_stat,
            "p_value": p_value,
            "adjusted_alpha": adjusted_alpha,
            "dsr": dsr,
            "brier_delta": brier_delta,
            "passes": passes,
        }
        details.append(detail)

        if passes and (promoted is None or dsr > promoted["dsr"]):
            promoted = {"id": result.get("config_id"), "dsr": dsr}

    promoted_id = promoted["id"] if promoted else None
    rejected_ids = [r.get("config_id") for r in results if r.get("config_id") != promoted_id]

    return {
        "promoted_id": promoted_id,
        "rejected_ids": rejected_ids,
        "details": details,
        "adjusted_alpha": adjusted_alpha,
    }


def write_review_record(
    db: Session,
    config_type: str,
    champion_id: str,
    challenger_results: list[dict[str, Any]],
    decision: dict[str, Any],
) -> DiscoveryConfigReview:
    """Persist a review decision row."""
    review = DiscoveryConfigReview(
        id=uuid_pk(),
        reviewed_at=now_utc(),
        config_type=config_type,
        champion_id=champion_id,
        challenger_results=challenger_results,
        promoted_id=decision.get("promoted_id"),
        rejected_ids=decision.get("rejected_ids", []),
        details_json={
            "adjusted_alpha": decision.get("adjusted_alpha"),
            "n_challengers": len(challenger_results),
            "summary": {
                r["config_id"]: {
                    "dsr": r.get("dsr"),
                    "brier_delta": r.get("brier_delta"),
                    "passes": r.get("passes"),
                }
                for r in decision.get("details", [])
            },
        },
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


def promote_config(db: Session, config_id: str) -> DiscoveryConfig | None:
    """Promote a config to active, returning the updated row or None on failure."""
    try:
        return activate_config(db, config_id)
    except ValueError:
        logger.warning("promote_config: config %s not found", config_id)
        return None


def discovery_review_inner(db: Session) -> None:
    """Run one full review cycle for all reviewable config types."""
    champions, challengers = collect_reviewable_configs(db)
    if not challengers:
        logger.info("discovery_review: no challenger configs to review")
        return

    by_type: dict[str, list[DiscoveryConfig]] = {}
    for challenger in challengers:
        by_type.setdefault(challenger.config_type, []).append(challenger)

    for config_type, chals in by_type.items():
        champion = champions.get(config_type)
        if champion is None:
            logger.info("discovery_review: no champion for %s; skipping", config_type)
            continue

        if config_type != "signal_weights":
            logger.info("discovery_review: %s is forward-only; skipping evaluation", config_type)
            continue

        min_preds = len(_resolved_predictions_for_champion(db, champion))
        if min_preds < 30:
            logger.info(
                "discovery_review: champion %s for %s has only %d resolved predictions; skipping",
                champion.id,
                config_type,
                min_preds,
            )
            continue

        results = evaluate_challengers(db, champion, chals)
        if not results:
            logger.info("discovery_review: no evaluable challengers for %s", config_type)
            continue

        decision = apply_multi_testing_correction(results)
        review = write_review_record(db, config_type, champion.id, results, decision)

        promoted_id = decision.get("promoted_id")
        if promoted_id:
            promoted = promote_config(db, promoted_id)
            if promoted is not None:
                if config_type == "signal_weights":
                    generate_weight_perturbations(db, base_config_id=promoted.id)
                logger.info(
                    "discovery_review: promoted config %s for %s (review %s)",
                    promoted_id,
                    config_type,
                    review.id,
                )
            else:
                logger.warning("discovery_review: promotion failed for config %s", promoted_id)
        else:
            logger.info("discovery_review: no promotion for %s (review %s)", config_type, review.id)

    logger.info("discovery_review: completed review cycle")
