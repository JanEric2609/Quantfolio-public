"""Weights for the lower-noise evidence series (ADR 0018, amendment of 2026-11-08).

Pure NumPy/pandas; shared by the live ledger (``decision.verification.daily_ledger``)
and the noise study that chose them (``lab.calibration.noise_study``), so both
use the same algebra.

* :func:`centred_rank_weights`: the frozen ranking weights, long leg +1, short -1.
* :func:`efficient_weights`: the minimum-variance portfolio with the same
  exposure to a signal and zero net weight (Ledoit, Wolf & Zhao 2019,
  "Efficient sorting"), under :func:`ledoit_wolf_cov`.
* :func:`stock_features` and :func:`nearest_controls`: matched controls by
  trailing beta, volatility and 12-1 momentum (Barber & Lyon 1997; Daniel,
  Grinblatt, Titman & Wermers 1997).
* :func:`precision_weight`: the inverse variance of a spread's trailing daily
  values, fixed at issue so the weighted daily mean keeps the conditional null.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

#: Trading days of history behind every weight.
LOOKBACK = 252
#: Days of the volatility feature.
VOL_DAYS = 63
#: Trading days skipped at the end of the momentum window (12-1 momentum).
SKIP_DAYS = 21
#: Trading days of a spread's trailing variance.
PRECISION_DAYS = 126
#: Matched controls per pick.
KNN = 5
#: Two stocks whose trailing daily returns correlate above this are one bet
#: (share classes such as GOOG/GOOGL); the later one in symbol order is
#: dropped, as Ledoit, Wolf & Zhao (2019) drop such pairs.
DUPLICATE_CORR = 0.95
#: A pick's precision weight is capped at this multiple of its run's median.
MAX_WEIGHT_RATIO = 4.0


def centred_rank_weights(score: np.ndarray) -> np.ndarray:
    """Centred ranks of *score* (ties averaged), long leg scaled to +1 and short leg to -1."""
    ranks = pd.Series(np.asarray(score, dtype=float)).rank(method="average").to_numpy()
    centred = ranks - (len(ranks) + 1) / 2.0
    long_total = centred[centred > 0].sum()
    return centred / long_total if long_total > 0 else np.zeros_like(centred)


def ledoit_wolf_cov(returns: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf (2004) shrinkage of the sample covariance of *returns* (days x assets)."""
    from sklearn.covariance import LedoitWolf

    return LedoitWolf().fit(returns).covariance_


def efficient_weights(signal: np.ndarray, cov: np.ndarray, target: float) -> np.ndarray:
    """``argmin w'Cw`` subject to ``1'w = 0`` and ``signal'w = target``.

    Lagrange: ``w = C^-1 A lam`` with ``A = [signal, 1]`` and
    ``A'C^-1 A lam = (target, 0)``.
    """
    a = np.column_stack([signal, np.ones_like(signal)])
    ci_a = np.linalg.solve(cov, a)
    lam = np.linalg.solve(a.T @ ci_a, np.array([target, 0.0]))
    return ci_a @ lam


def ols_betas(returns: np.ndarray, market: np.ndarray) -> np.ndarray:
    """OLS slope of each column of *returns* on *market* (same days)."""
    mc = market - market.mean()
    var = float(mc @ mc)
    if var <= 0:
        return np.zeros(returns.shape[1])
    return (mc @ (returns - returns.mean(axis=0))) / var


def stock_features(returns: np.ndarray, market: np.ndarray) -> np.ndarray:
    """z-scored (beta, volatility, 12-1 momentum) per column from :data:`LOOKBACK` days of returns."""
    beta = ols_betas(returns, market)
    vol = returns[-VOL_DAYS:].std(axis=0, ddof=1)
    growth = np.cumprod(1.0 + returns, axis=0)
    mom = growth[-SKIP_DAYS - 1] / growth[0] - 1.0
    feats = np.column_stack([beta, vol, mom])
    sd = feats.std(axis=0)
    return (feats - feats.mean(axis=0)) / np.where(sd > 0, sd, 1.0)


def nearest_controls(features: np.ndarray, pick: int, pool: np.ndarray, k: int = KNN) -> np.ndarray:
    """The *k* members of *pool* nearest to *pick* in feature space (ties by index)."""
    dist = np.linalg.norm(features[pool] - features[pick], axis=1)
    return pool[np.argsort(dist, kind="stable")[:k]]


def near_duplicate_pairs(returns: np.ndarray, threshold: float = DUPLICATE_CORR) -> list[tuple[int, int]]:
    """Column pairs ``(j, k)``, ``j < k``, whose daily returns correlate above *threshold*."""
    if returns.shape[1] < 2:
        return []
    sd = returns.std(axis=0)
    ok = sd > 0
    z = np.zeros_like(returns)
    z[:, ok] = (returns[:, ok] - returns[:, ok].mean(axis=0)) / sd[ok]
    corr = (z.T @ z) / len(returns)
    upper = np.triu(corr > threshold, k=1)
    return [(int(j), int(k)) for j, k in zip(*np.nonzero(upper))]


def near_duplicates(returns: np.ndarray, threshold: float = DUPLICATE_CORR) -> set[int]:
    """Columns to drop so no near-duplicate pair remains: the later column of each pair."""
    drop: set[int] = set()
    for j, k in near_duplicate_pairs(returns, threshold):
        if j not in drop:
            drop.add(k)
    return drop


def cap_weights(weights: list[float], ratio: float = MAX_WEIGHT_RATIO) -> list[float]:
    """Cap each weight at *ratio* times the median (one quiet pick cannot carry a day)."""
    if not weights:
        return weights
    ceiling = ratio * float(np.median(weights))
    return [min(w, ceiling) for w in weights]


def precision_weight(spread: np.ndarray, days: int = PRECISION_DAYS) -> float | None:
    """``1 / var`` of the last *days* values of *spread*; None when it cannot be estimated."""
    tail = np.asarray(spread, dtype=float)[-days:]
    if len(tail) < 2:
        return None
    var = float(np.var(tail, ddof=1))
    return 1.0 / var if var > 0 else None


__all__ = [
    "DUPLICATE_CORR",
    "KNN",
    "MAX_WEIGHT_RATIO",
    "cap_weights",
    "near_duplicate_pairs",
    "near_duplicates",
    "LOOKBACK",
    "PRECISION_DAYS",
    "centred_rank_weights",
    "efficient_weights",
    "ledoit_wolf_cov",
    "nearest_controls",
    "ols_betas",
    "precision_weight",
    "stock_features",
]
