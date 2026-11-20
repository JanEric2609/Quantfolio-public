"""Admission filter for backtest-selected candidates (ADR 0025, plan 2026-11-10 WS4).

The Deflated Sharpe and PBO checks that used to sit in the graduation gate
moved here. They are the right tool for a strategy picked from a backtest
search: they ask whether the winner of N tries beats the luckiest of N skill-less
ones. They are the wrong tool for a forward paper record, which the capital
ladder (``decision/graduation/ladder.py``) now judges with e-processes.

Two corrections over the graduation version:

* the Sharpe is computed on **excess** returns over the benchmark, not raw
  returns, so a strategy is not credited for the market's own drift;
* N is the effective number of trials of the families the candidate was
  selected from (ADR 0025 WS2), and the null variance of the Sharpe is the
  observed cross-trial variance when the trials' Sharpes are known (the
  ``1/(T-1)`` default of ``quant_metrics.deflated_sharpe_ratio`` otherwise).

A pass decides only whether a lab strategy may be **registered** as a ladder
candidate, a future third slot that is out of scope for now. It never sets a
weight.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

import numpy as np
from sqlalchemy.orm import Session

from app.foundation import quant_metrics as qm

MIN_DSR = 0.95
MAX_PBO = 0.50
MIN_OBSERVATIONS = 60


@dataclass
class AdmissionResult:
    admitted: bool
    reason: str
    n_trials_effective: int
    families: list[str]
    dsr: float | None
    variance_sharpe: float | None
    pbo: float | None
    min_track_record_length: float | None
    observations: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def observed_sharpe_variance(trial_sharpes: Sequence[float] | None) -> float | None:
    """Per-period cross-sectional variance of the trials' Sharpe ratios."""
    if not trial_sharpes:
        return None
    arr = np.asarray([s for s in trial_sharpes if s is not None and np.isfinite(s)], dtype=float)
    if len(arr) < 2:
        return None
    var = float(arr.var(ddof=1))
    return var if var > 0 else None


def backtest_admission(
    db: Session,
    excess_returns: Sequence[float],
    *,
    families: Sequence[str],
    trial_sharpes: Sequence[float] | None = None,
    returns_matrix: np.ndarray | None = None,
    periods_per_year: int = 252,
) -> AdmissionResult:
    """Grade one backtest-selected candidate on its excess returns.

    ``trial_sharpes`` are the per-period Sharpes of every configuration it was
    selected among; ``returns_matrix`` (T x N) the same trials' excess returns
    for the CSCV probability of backtest overfitting.
    """
    x = [float(v) for v in excess_returns if v is not None and np.isfinite(v)]
    n_eff = qm.effective_n_trials(db, list(families))
    var = observed_sharpe_variance(trial_sharpes)
    if len(x) < MIN_OBSERVATIONS:
        return AdmissionResult(
            False, f"{len(x)} of {MIN_OBSERVATIONS} observations", n_eff, list(families),
            None, var, None, None, len(x),
        )
    dsr = float(qm.deflated_sharpe_ratio(x, n_eff, variance_sharpe=var, periods_per_year=periods_per_year))
    mtrl = float(qm.min_track_record_length(x, periods_per_year=periods_per_year))
    pbo = None
    if returns_matrix is not None and np.asarray(returns_matrix).ndim == 2 and np.asarray(returns_matrix).shape[1] >= 2:
        pbo = float(qm.probability_of_backtest_overfitting(np.asarray(returns_matrix, dtype=float)))
    reasons = []
    if dsr < MIN_DSR:
        reasons.append(f"Deflated Sharpe {dsr:.2%} < {MIN_DSR:.0%} at N_eff {n_eff}")
    if pbo is not None and pbo > MAX_PBO:
        reasons.append(f"PBO {pbo:.0%} > {MAX_PBO:.0%}")
    if np.isfinite(mtrl) and len(x) < mtrl:
        reasons.append(f"{len(x)} observations < minimum track record {mtrl:.0f}")
    admitted = not reasons
    return AdmissionResult(
        admitted, "admitted" if admitted else "; ".join(reasons), n_eff, list(families),
        dsr, var, pbo, mtrl if np.isfinite(mtrl) else None, len(x),
    )
