"""IC decay monitoring for AlphaCrafter factors (Phase 4).

Tracks each factor's rolling information coefficient and auto-retires it when
the signal has decayed — concretely, when the most recent ``tau`` IC
observations are all below the (absolute) threshold, the note's
"IC < threshold for τ periods" rule.

The rolling IC series comes from two sources, preferring live data:

* ``AlphaSignal.ic_window_value`` rows for the factor (chronological), if any
  have been written by an online IC tracker;
* otherwise the ``ic_series`` the Miner stored in
  ``FactorsLibrary.ic_summary_json`` at validation time.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.foundation.models.entities import AlphaSignal, FactorsLibrary
from app.lab.alphacrafter.miner import decay_halflife_days

logger = logging.getLogger(__name__)

DEFAULT_MIN_IC = 0.02
DEFAULT_TAU = 3


@dataclass
class DecayAnalysis:
    """Analysis of a factor's IC decay."""

    factor_id: str
    half_life_days: float
    ic_current: float
    ic_3m_avg: float
    decay_rate: float
    recommend_retire: bool
    analysis_date: datetime
    n_obs: int = 0


def _ic_series_for_factor(db: Session, factor: FactorsLibrary, lookback: int | None) -> list[float]:
    """Return a factor's rolling IC series, oldest→newest.

    Prefers persisted ``AlphaSignal.ic_window_value`` rows; falls back to the
    Miner's stored ``ic_series``. Truncated to the most recent ``lookback``.
    """
    rows = (
        db.execute(
            select(AlphaSignal.ic_window_value)
            .where(
                AlphaSignal.factor_id == factor.id,
                AlphaSignal.ic_window_value.is_not(None),
            )
            .order_by(AlphaSignal.ts.asc())
        )
        .scalars()
        .all()
    )
    series = [float(x) for x in rows if x is not None]

    if not series:
        try:
            meta = json.loads(factor.ic_summary_json) if factor.ic_summary_json else {}
            series = [float(x) for x in (meta.get("ic_series") or [])]
        except (json.JSONDecodeError, TypeError, ValueError):
            series = []

    if lookback is not None and lookback > 0:
        series = series[-lookback:]
    return series


def _decay_rate(series: list[float]) -> float:
    """Fractional drop in |IC| from the first half of the window to the second.

    Positive ⇒ the signal is weakening. 0 when there is no early signal.
    """
    if len(series) < 4:
        return 0.0
    half = len(series) // 2
    early = float(np.mean(np.abs(series[:half])))
    recent = float(np.mean(np.abs(series[half:])))
    if early <= 0:
        return 0.0
    return (early - recent) / early


async def analyze_decay(
    db: Session,
    factor_id,
    lookback_days: int = 90,
    *,
    min_ic: float = DEFAULT_MIN_IC,
    tau: int = DEFAULT_TAU,
) -> DecayAnalysis:
    """Analyze IC decay for a factor and recommend retirement.

    Retirement is recommended when the most recent ``tau`` IC observations are
    all below ``min_ic`` in absolute value (signal gone for τ periods).

    Args:
        db: Database session.
        factor_id: Factor id to analyze.
        lookback_days: Window of recent IC observations to consider.
        min_ic: Absolute IC threshold below which the signal counts as dead.
        tau: Number of consecutive recent sub-threshold periods to retire on.
    """
    now = datetime.now(UTC)
    factor = db.get(FactorsLibrary, str(factor_id))
    empty = DecayAnalysis(
        factor_id=str(factor_id),
        half_life_days=float("inf"),
        ic_current=0.0,
        ic_3m_avg=0.0,
        decay_rate=0.0,
        recommend_retire=False,
        analysis_date=now,
        n_obs=0,
    )
    if factor is None:
        return empty

    series = _ic_series_for_factor(db, factor, lookback_days)
    if not series:
        return empty

    ic_current = float(series[-1])
    ic_3m_avg = float(np.mean(series))

    # Half-life: prefer the value the Miner already estimated, else recompute.
    half_life = float("inf")
    try:
        meta = json.loads(factor.ic_summary_json) if factor.ic_summary_json else {}
        stored = meta.get("decay_halflife_days")
        hl = stored if stored is not None else decay_halflife_days(pd.Series(series))
        if hl is not None and not math.isinf(float(hl)):
            half_life = float(hl)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    recent = series[-tau:]
    recommend_retire = len(recent) >= tau and all(abs(x) < min_ic for x in recent)

    return DecayAnalysis(
        factor_id=str(factor_id),
        half_life_days=half_life,
        ic_current=ic_current,
        ic_3m_avg=ic_3m_avg,
        decay_rate=_decay_rate(series),
        recommend_retire=recommend_retire,
        analysis_date=now,
        n_obs=len(series),
    )


async def auto_retire_factors(
    db: Session,
    min_ic_threshold: float = DEFAULT_MIN_IC,
    *,
    tau: int = DEFAULT_TAU,
    lookback_days: int = 90,
) -> list[str]:
    """Retire active factors whose IC has decayed below threshold for τ periods.

    Returns the ids of the factors retired.
    """
    active_factors = (
        db.execute(select(FactorsLibrary).where(FactorsLibrary.retired_at.is_(None)))
        .scalars()
        .all()
    )

    retired_ids: list[str] = []
    now = datetime.now(UTC)
    for factor in active_factors:
        analysis = await analyze_decay(
            db, factor.id, lookback_days=lookback_days, min_ic=min_ic_threshold, tau=tau
        )
        if analysis.recommend_retire:
            db.execute(
                update(FactorsLibrary).where(FactorsLibrary.id == factor.id).values(retired_at=now)
            )
            retired_ids.append(factor.id)

    db.commit()
    return retired_ids
