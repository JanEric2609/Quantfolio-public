"""Target allocations from covariance alone, and band rebalancing toward them.

Mean-variance optimisation on historical mean returns is an error maximiser
(Michaud 1989; Chopra and Ziemba 1993: errors in means cost about ten times
more than errors in covariances), and for a book of three to five broad ETFs
it adds nothing a fixed allocation does not (DeMiguel, Garlappi and Uppal
2009). Every candidate here therefore uses only the covariance matrix,
shrunk toward a scaled identity matrix by Ledoit and Wolf (2004), long-only, fully invested and optionally capped per instrument:

* equal weight, the 1/N benchmark;
* inverse volatility;
* minimum variance;
* equal risk contribution (ERC, Maillard, Roncalli and Teiletche 2010);
* hierarchical risk parity (HRP, Lopez de Prado 2016).

Rebalancing toward a target follows the evidence on bands (Vanguard 2010,
2015): new money goes to the most underweight lines first, and a line is sold
only when it sits more than a band above target and a year of contributions
would not bring it back, because every sale is a taxable event. The monthly
plan applies the same two functions at sleeve level.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from app.foundation.quant_metrics import TRADING_DAYS_PER_YEAR

DRIFT_HORIZON_MONTHS = 12

METHOD_LABELS: dict[str, str] = {
    "equal": "Equal weight (1/N)",
    "inverse_vol": "Inverse volatility",
    "min_variance": "Minimum variance",
    "erc": "Equal risk contribution",
    "hrp": "Hierarchical risk parity",
}


# ---------------------------------------------------------------------------
# Contribution-first allocation and band sales (shared with the monthly plan)
# ---------------------------------------------------------------------------


def allocate_contribution(
    current: dict[str, float], targets_pct: dict[str, float], contribution: float,
) -> dict[str, float]:
    """Split *contribution* across lines without selling anything (water-filling).

    Each line's shortfall against its target (measured on the post-contribution
    total) is filled first, pro rata if the money does not cover all of them;
    anything left over follows the target weights. Lines with a 0 % target
    never receive money.
    """
    total_after = sum(current.values()) + contribution
    shortfall = {
        k: max(0.0, targets_pct[k] / 100.0 * total_after - current.get(k, 0.0))
        for k in targets_pct
    }
    need = sum(shortfall.values())
    if contribution <= 0:
        return {k: 0.0 for k in targets_pct}
    if need >= contribution:
        return {k: contribution * shortfall[k] / need for k in targets_pct}
    leftover = contribution - need
    weight_sum = sum(targets_pct.values()) or 1.0
    return {k: shortfall[k] + leftover * targets_pct[k] / weight_sum for k in targets_pct}


def drift_sales(
    current: dict[str, float],
    targets_pct: dict[str, float],
    unlocked: dict[str, bool],
    contribution: float,
    band_pp: float,
    horizon_months: int = DRIFT_HORIZON_MONTHS,
) -> dict[str, float]:
    """How much of each line to sell, if any, to bring it back to target.

    A line is sold only when all of these hold:

    * it is unlocked (a locked line's positions are never sold);
    * after this month's contribution it sits more than *band_pp* above its
      target;
    * *horizon_months* of contributions, all going to the other lines (and
      ignoring returns), would still leave it above target + band.

    It is then sold down to its target on the book after this month's
    contribution. Returns only lines with a positive sale.
    """
    book = sum(current.values())
    total_after = book + max(0.0, contribution)
    future_total = book + max(0.0, contribution) * max(1, horizon_months)
    sales: dict[str, float] = {}
    if total_after <= 0:
        return sales
    for k, target in targets_pct.items():
        held = current.get(k, 0.0)
        if not unlocked.get(k) or held <= 0:
            continue
        limit = target + band_pp
        if 100.0 * held / total_after <= limit or 100.0 * held / future_total <= limit:
            continue
        sale = held - target / 100.0 * total_after
        if sale > 0:
            sales[k] = sale
    return sales


# ---------------------------------------------------------------------------
# Covariance-only candidates
# ---------------------------------------------------------------------------


def shrunk_covariance(returns: pd.DataFrame) -> tuple[np.ndarray, float]:
    """Ledoit-Wolf covariance of daily returns (annualised) and its shrinkage intensity."""
    from sklearn.covariance import LedoitWolf

    lw = LedoitWolf().fit(returns.values)
    return lw.covariance_ * TRADING_DAYS_PER_YEAR, float(lw.shrinkage_)


def portfolio_stats(weights: np.ndarray, cov: np.ndarray, current: np.ndarray | None = None) -> dict[str, Any]:
    """Volatility, risk contributions, diversification ratio, effective N, turnover."""
    w = np.asarray(weights, dtype=float)
    var = float(w @ cov @ w)
    vol = math.sqrt(max(var, 0.0))
    marginal = cov @ w
    rc = (w * marginal / var) if var > 0 else np.zeros_like(w)
    asset_vol = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    out: dict[str, Any] = {
        "volatility": vol,
        "risk_contributions": rc.tolist(),
        "diversification_ratio": float(w @ asset_vol / vol) if vol > 0 else None,
        "effective_n": float(1.0 / np.sum(w ** 2)) if np.sum(w ** 2) > 0 else None,
    }
    if current is not None:
        out["turnover"] = float(0.5 * np.abs(w - current).sum())
    return out


def _skfolio_weights(kind: str, returns: pd.DataFrame, max_weight: float | None) -> np.ndarray:
    from skfolio import RiskMeasure
    from skfolio.moments import LedoitWolf
    from skfolio.optimization import HierarchicalRiskParity, MeanRisk, ObjectiveFunction, RiskBudgeting
    from skfolio.prior import EmpiricalPrior

    prior = EmpiricalPrior(covariance_estimator=LedoitWolf())
    cap = 1.0 if max_weight is None else float(max_weight)
    if kind == "min_variance":
        model = MeanRisk(objective_function=ObjectiveFunction.MINIMIZE_RISK, risk_measure=RiskMeasure.VARIANCE,
                         prior_estimator=prior, min_weights=0.0, max_weights=cap)
    elif kind == "erc":
        model = RiskBudgeting(risk_measure=RiskMeasure.VARIANCE, prior_estimator=prior, min_weights=0.0, max_weights=cap)
    elif kind == "hrp":
        model = HierarchicalRiskParity(risk_measure=RiskMeasure.VARIANCE, prior_estimator=prior, max_weights=cap)
    else:
        raise ValueError(kind)
    model.fit(returns)
    return np.asarray(model.weights_, dtype=float)


def allocation_candidates(
    returns: pd.DataFrame, current_weights: dict[str, float], *, max_weight: float | None = None,
) -> dict[str, Any]:
    """Every covariance-only candidate for the instruments in *returns* (daily, EUR).

    ``current_weights`` are fractions of the priced book; a cap below 1/N is
    raised to 1/N so the problem stays feasible.
    """
    assets = list(returns.columns)
    n = len(assets)
    if n == 0 or len(returns) < 2:
        return {"available": False, "reason": "Not enough price history.", "methods": {}}
    if max_weight is not None:
        max_weight = max(float(max_weight), 1.0 / n)
    cov, shrinkage = shrunk_covariance(returns)
    cur = np.array([float(current_weights.get(a, 0.0)) for a in assets])
    if cur.sum() > 0:
        cur = cur / cur.sum()
    vol = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
    raw: dict[str, np.ndarray] = {
        "equal": np.full(n, 1.0 / n),
        "inverse_vol": (1.0 / vol) / np.sum(1.0 / vol),
    }
    errors: dict[str, str] = {}
    if n == 1:
        raw.update({k: np.ones(1) for k in ("min_variance", "erc", "hrp")})
    else:
        for kind in ("min_variance", "erc", "hrp"):
            try:
                raw[kind] = _skfolio_weights(kind, returns, max_weight)
            except Exception as exc:  # noqa: BLE001 - one failed solver must not hide the rest
                errors[kind] = str(exc)
    if max_weight is not None:
        for kind in ("equal", "inverse_vol"):
            raw[kind] = _cap_weights(raw[kind], max_weight)
    methods: dict[str, Any] = {}
    for kind, w in raw.items():
        w = np.clip(w, 0.0, None)
        w = w / w.sum() if w.sum() > 0 else w
        stats = portfolio_stats(w, cov, cur)
        methods[kind] = {
            "label": METHOD_LABELS[kind],
            "weights": {a: float(x) for a, x in zip(assets, w)},
            **stats,
            "risk_contributions": dict(zip(assets, stats["risk_contributions"])),
        }
    current = portfolio_stats(cur, cov) if cur.sum() > 0 else None
    if current is not None:
        current["risk_contributions"] = dict(zip(assets, current["risk_contributions"]))
        current["weights"] = {a: float(x) for a, x in zip(assets, cur)}
    idx = returns.index
    return {
        "available": True,
        "assets": assets,
        "methods": methods,
        "current": current,
        "errors": errors,
        "max_weight": max_weight,
        "covariance": {
            "estimator": "Ledoit-Wolf",
            "shrinkage": shrinkage,
            "samples": int(len(returns)),
            "start": str(idx[0])[:10],
            "end": str(idx[-1])[:10],
            "annualised_volatility": dict(zip(assets, vol.tolist())),
        },
    }


def _cap_weights(w: np.ndarray, cap: float) -> np.ndarray:
    """Clip at *cap* and hand the excess to the uncapped lines in proportion."""
    w = np.asarray(w, dtype=float).copy()
    for _ in range(len(w) + 1):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        free = ~over & (w < cap)
        if not free.any():
            break
        w[free] += excess * w[free] / w[free].sum()
    return w
