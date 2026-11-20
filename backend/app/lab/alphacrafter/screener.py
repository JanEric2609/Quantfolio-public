"""AlphaCrafter Screener: regime-conditioned factor selection (Phase 4).

Reads the latest regime snapshot (Plan 1 — HMM classifier + crisis gate) and
selects the factors best suited to it. Each candidate is scored by:

* **Base quality** — the factor's own ``|IC|`` and ``ICIR`` from
  ``FactorsLibrary.ic_summary_json``. More consistent factors (higher |ICIR|)
  score higher.
* **Regime affinity** — an economically-motivated multiplier per factor
  *category* conditioned on the current regime: momentum thrives in a bull
  trend, quality/low-vol are defensive in bear/crisis, value works in
  sideways/recovery.
* **Crisis gate** — when the crisis flag is set, LLM-sourced factors are
  suppressed entirely (we do not trust speculative, freshly-mined signals in a
  tail event) and defensive categories are favoured. This is the note's
  *feature-validity-to-policy* gate.

A correlation-aware greedy pass then drops a candidate whose IC series is highly
correlated with an already-selected factor, so the final basket is diversified
rather than five flavours of the same bet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.foundation.models.entities import FactorsLibrary, ScreenerRun
from app.lab.alphacrafter.shared_memory import SharedMemoryH
from app.foundation.data_backbone.regime_store import RegimeStore

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 5
DEFAULT_MAX_CORRELATION = 0.8

# Regime affinity multipliers per factor category. Inner keys are regime labels;
# "crisis" is applied (in place of the regime column) when the crisis gate
# fires. Values > 1 favour the category in that regime, < 1 penalise it.
_REGIME_AFFINITY: dict[str, dict[str, float]] = {
    "momentum": {"bull": 1.3, "sideways": 0.9, "bear": 0.6, "crisis": 0.4},
    "value": {"bull": 0.9, "sideways": 1.2, "bear": 1.1, "crisis": 1.0},
    "quality": {"bull": 1.0, "sideways": 1.1, "bear": 1.3, "crisis": 1.4},
    "size": {"bull": 1.1, "sideways": 1.0, "bear": 0.8, "crisis": 0.6},
    "low_vol": {"bull": 0.8, "sideways": 1.0, "bear": 1.3, "crisis": 1.5},
    "unknown": {"bull": 1.0, "sideways": 1.0, "bear": 1.0, "crisis": 0.9},
}


@dataclass
class ScreenerSelection:
    """Screener run result."""

    screener_run_id: str
    regime_label: str | None
    selected_factor_ids: list[str]
    regime_score: float
    timestamp: datetime
    crisis: bool = False
    scores: dict = field(default_factory=dict)


@dataclass
class _ScoredFactor:
    """A candidate factor with its computed screening score."""

    factor_id: str
    name: str
    source: str
    category: str
    ic: float
    icir: float
    base: float
    affinity: float
    score: float
    ic_series: list[float]
    rejected_reason: str | None = None


# --- Pure helpers (synchronous, unit-testable) --------------------------------


def infer_category(name: str, formula: str = "") -> str:
    """Infer a factor's category from its name (and formula text) for gating.

    Categories drive the regime affinity table. Seed factors are named after
    their category (``momentum_12_1`` etc.); LLM/DSL factors are classified by
    keyword and fall back to ``"unknown"`` (neutral affinity).
    """
    text = f"{name} {formula}".lower()
    if any(k in text for k in ("mom", "trend", "reversal")):
        return "momentum"
    if any(k in text for k in ("value", "_ep", "earnings_price", "pe_ratio", "book", "_bp")):
        return "value"
    if any(k in text for k in ("qual", "roe", "roa", "margin", "profit")):
        return "quality"
    if any(k in text for k in ("size", "market_cap", "mktcap", "_mc")):
        return "size"
    if any(k in text for k in ("vol", "risk", "beta", "drawdown")):
        return "low_vol"
    return "unknown"


def regime_affinity(category: str, regime_label: str | None, crisis: bool) -> float:
    """Look up the affinity multiplier for a category in the current regime."""
    row = _REGIME_AFFINITY.get(category, _REGIME_AFFINITY["unknown"])
    if crisis:
        return row["crisis"]
    if regime_label in row:
        return row[regime_label]
    return 1.0  # Unknown / missing regime -> neutral.


def base_quality(ic: float, icir: float) -> float:
    """Combine |IC| and |ICIR| into a single base-quality score.

    ``|IC|`` is the predictive strength; the ``(1 + |ICIR|)`` term rewards
    consistency, so a factor with the same mean IC but a steadier signal ranks
    higher. ICIR is clipped to keep degenerate (tiny-dispersion) factors from
    dominating.
    """
    return abs(ic) * (1.0 + min(abs(icir), 3.0))


def series_correlation(a: list[float], b: list[float]) -> float:
    """Pearson correlation of two IC series, aligned on their common tail.

    Returns 0.0 when there is insufficient overlap (<3 points) or either series
    is constant, i.e. treat as uncorrelated so the candidate is kept.
    """
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    av = np.asarray(a[-n:], dtype=float)
    bv = np.asarray(b[-n:], dtype=float)
    if np.std(av) == 0 or np.std(bv) == 0:
        return 0.0
    c = np.corrcoef(av, bv)[0, 1]
    return 0.0 if np.isnan(c) else float(c)


def _score_candidate(
    factor: FactorsLibrary,
    regime_label: str | None,
    crisis: bool,
) -> _ScoredFactor:
    """Parse a factor's stored metrics and compute its regime-conditioned score."""
    try:
        metrics = json.loads(factor.ic_summary_json) if factor.ic_summary_json else {}
    except (json.JSONDecodeError, TypeError):
        metrics = {}
    ic = float(metrics.get("ic", 0.0) or 0.0)
    icir = float(metrics.get("icir", 0.0) or 0.0)
    ic_series = [float(x) for x in (metrics.get("ic_series") or [])]

    formula = ""
    try:
        formula = json.loads(factor.formula_json).get("formula", "") if factor.formula_json else ""
    except (json.JSONDecodeError, TypeError):
        formula = ""

    category = infer_category(factor.name, formula)
    affinity = regime_affinity(category, regime_label, crisis)
    base = base_quality(ic, icir)

    scored = _ScoredFactor(
        factor_id=factor.id,
        name=factor.name,
        source=factor.source,
        category=category,
        ic=ic,
        icir=icir,
        base=base,
        affinity=affinity,
        score=base * affinity,
        ic_series=ic_series,
    )

    # Crisis gate: do not deploy freshly-mined LLM signals in a tail event.
    if crisis and factor.source == "llm":
        scored.score = 0.0
        scored.rejected_reason = "crisis_llm_suppressed"

    return scored


def _select_diversified(
    scored: list[_ScoredFactor],
    top_n: int,
    max_correlation: float,
) -> tuple[list[_ScoredFactor], list[_ScoredFactor]]:
    """Greedily pick the highest-scoring, mutually-decorrelated factors.

    Iterates candidates by descending score, skipping any whose IC series is
    correlated above ``max_correlation`` with an already-selected factor.
    Returns ``(selected, rejected)``.
    """
    selected: list[_ScoredFactor] = []
    rejected: list[_ScoredFactor] = []
    for cand in scored:
        if cand.rejected_reason is not None:
            rejected.append(cand)
            continue
        if cand.score <= 0:
            cand.rejected_reason = "non_positive_score"
            rejected.append(cand)
            continue
        if len(selected) >= top_n:
            cand.rejected_reason = "below_top_n_cutoff"
            rejected.append(cand)
            continue
        clash = next(
            (
                s
                for s in selected
                if abs(series_correlation(cand.ic_series, s.ic_series)) > max_correlation
            ),
            None,
        )
        if clash is not None:
            cand.rejected_reason = f"correlated_with:{clash.name}"
            rejected.append(cand)
            continue
        selected.append(cand)
    return selected, rejected


async def run_screener(
    db: Session,
    candidate_factor_ids: list[str],
    regime_label: str | None = None,
    *,
    crisis: bool | None = None,
    top_n: int = DEFAULT_TOP_N,
    max_correlation: float = DEFAULT_MAX_CORRELATION,
) -> ScreenerSelection:
    """Run the Screener: regime-condition + diversify the Miner's candidates.

    Stages:
    1. Read the latest regime snapshot (label + crisis flag) unless overridden.
    2. Score each candidate by base quality × regime affinity; suppress
       LLM-sourced factors entirely under crisis.
    3. Greedily select the top ``top_n`` decorrelated factors.
    4. Persist a ``ScreenerRun`` row with the full per-factor score breakdown.

    Args:
        db: Database session.
        candidate_factor_ids: ``FactorsLibrary`` ids to consider (from the Miner).
        regime_label: Override regime label (for testing). If ``None``, read from
            the latest regime snapshot.
        crisis: Override the crisis flag (for testing). If ``None``, read from the
            latest regime snapshot payload.
        top_n: Maximum number of factors to select.
        max_correlation: Drop a candidate whose IC series correlates above this
            (absolute) threshold with an already-selected factor.

    Returns:
        :class:`ScreenerSelection` with the selected factors and score detail.
    """
    # Normalise ids to strings (FactorsLibrary.id is a uuid string; the API may
    # hand us UUID objects).
    ids = [str(x) for x in candidate_factor_ids]

    # Resolve regime context.
    snapshot = RegimeStore(db).get_latest_snapshot()
    if regime_label is None and snapshot:
        regime_label = snapshot.get("label")
    if crisis is None:
        crisis = bool(snapshot.get("payload", {}).get("crisis", False)) if snapshot else False

    candidates = (
        db.execute(select(FactorsLibrary).where(FactorsLibrary.id.in_(ids))).scalars().all()
        if ids
        else []
    )

    scored = sorted(
        (_score_candidate(c, regime_label, crisis) for c in candidates),
        key=lambda s: s.score,
        reverse=True,
    )
    selected, rejected = _select_diversified(scored, top_n, max_correlation)

    selected_ids = [s.factor_id for s in selected]
    regime_score = float(np.mean([s.score for s in selected])) if selected else 0.0

    scores_payload = {
        "regime_label": regime_label,
        "crisis": crisis,
        "top_n": top_n,
        "max_correlation": max_correlation,
        "regime_score": regime_score,
        "selected": [_scored_to_dict(s) for s in selected],
        "rejected": [_scored_to_dict(s) for s in rejected],
    }

    screener_run = ScreenerRun(
        regime_label=regime_label,
        selected_factor_ids=",".join(selected_ids),
        scores_json=json.dumps(scores_payload),
    )
    db.add(screener_run)
    db.commit()

    return ScreenerSelection(
        screener_run_id=screener_run.id,
        regime_label=regime_label,
        selected_factor_ids=selected_ids,
        regime_score=regime_score,
        timestamp=screener_run.ts,
        crisis=crisis,
        scores=scores_payload,
    )


def _scored_to_dict(s: _ScoredFactor) -> dict:
    """Serialise a scored factor for ``ScreenerRun.scores_json`` (drops the series)."""
    return {
        "factor_id": s.factor_id,
        "name": s.name,
        "source": s.source,
        "category": s.category,
        "ic": s.ic,
        "icir": s.icir,
        "base": s.base,
        "affinity": s.affinity,
        "score": s.score,
        "rejected_reason": s.rejected_reason,
    }


class ScreenerAgent:
    """Screener agent that reads regime from SharedMemoryH and selects factors."""

    def run(self, H: SharedMemoryH, db: Session) -> SharedMemoryH:
        """Run regime-conditioned factor selection.

        Reads regime from H.market_state, scores factors by regime affinity,
        applies crisis gate (suppress LLM factors), and performs greedy
        decorrelation. Writes results to H.screener_outputs, H.regime_assessment,
        and H.agent_history.

        Args:
            H: SharedMemoryH with market_state and factor_states populated.
            db: Database session (for persisting ScreenerRun).

        Returns:
            Updated SharedMemoryH with screener outputs.
        """
        regime_label = H.market_state.regime_label
        crisis = H.market_state.crisis
        factor_states = H.factor_states

        ids = [fs.id for fs in factor_states]

        candidates = (
            db.execute(select(FactorsLibrary).where(FactorsLibrary.id.in_(ids))).scalars().all()
            if ids
            else []
        )

        id_to_state = {fs.id: fs for fs in factor_states}

        scored = []
        for c in candidates:
            fs = id_to_state.get(c.id)
            metrics_dict = {
                "ic": fs.metrics.ic,
                "icir": fs.metrics.icir,
                "ic_series": [],
            } if fs else {}

            category = fs.category if fs else infer_category(c.name, "")
            affinity = regime_affinity(category, regime_label, crisis)
            base = base_quality(
                float(metrics_dict.get("ic", 0.0)),
                float(metrics_dict.get("icir", 0.0)),
            )

            scored_f = _ScoredFactor(
                factor_id=c.id,
                name=c.name,
                source=c.source,
                category=category,
                ic=float(metrics_dict.get("ic", 0.0)),
                icir=float(metrics_dict.get("icir", 0.0)),
                base=base,
                affinity=affinity,
                score=base * affinity,
                ic_series=[],
            )

            if crisis and c.source == "llm":
                scored_f.score = 0.0
                scored_f.rejected_reason = "crisis_llm_suppressed"

            scored.append(scored_f)

        scored.sort(key=lambda s: s.score, reverse=True)
        selected, rejected = _select_diversified(scored, DEFAULT_TOP_N, DEFAULT_MAX_CORRELATION)

        selected_ids = [s.factor_id for s in selected]
        regime_score = float(np.mean([s.score for s in selected])) if selected else 0.0

        scores_payload = {
            "regime_label": regime_label,
            "crisis": crisis,
            "top_n": DEFAULT_TOP_N,
            "max_correlation": DEFAULT_MAX_CORRELATION,
            "regime_score": regime_score,
            "selected": [_scored_to_dict(s) for s in selected],
            "rejected": [_scored_to_dict(s) for s in rejected],
        }

        screener_run = ScreenerRun(
            regime_label=regime_label,
            selected_factor_ids=",".join(selected_ids),
            scores_json=json.dumps(scores_payload),
        )
        db.add(screener_run)
        db.commit()

        H.screener_outputs = {
            "screener_run_id": screener_run.id,
            "selected_factor_ids": selected_ids,
            "regime_label": regime_label,
            "crisis": crisis,
            "regime_score": regime_score,
            "rejected": [_scored_to_dict(s) for s in rejected],
        }

        H.regime_assessment = {
            "regime_label": regime_label,
            "crisis": crisis,
            "selected_factor_ids": selected_ids,
            "regime_score": regime_score,
            "rejected": [_scored_to_dict(s) for s in rejected],
        }

        H.append_history(
            agent="screener",
            action="run",
            details={
                "regime_label": regime_label,
                "crisis": crisis,
                "n_selected": len(selected_ids),
                "n_rejected": len(rejected),
            },
        )

        return H
