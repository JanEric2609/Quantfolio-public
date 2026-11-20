"""Fitting: ridge from per-month Gram matrices, gradient boosting on a row sample.

Ridge needs only X'X, X'y and y'y, so one pass over the panel stores them per
month (a few MB for 60 years) and every fold's fit, and its validation error,
is a sum over months plus one small solve: exact, and no full-panel matrix is
ever held in memory. LightGBM does need rows, so the same pass keeps a
fixed-seed uniform sample of at most ``GBM_MAX_ROWS``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np

from app.lab.pooled_model.panel import Chunk
from app.lab.pooled_model.spec import (
    GBM_EARLY_STOPPING,
    GBM_MAX_ROWS,
    GBM_MAX_TREES,
    GBM_PARAMS,
    PURGE_MONTHS,
    RIDGE_LAMBDAS,
    SEED,
    VALIDATION_MONTHS,
)

# LightGBM threads: the study runs next to the app on a shared container.
GBM_THREADS = 2
# A training window too short to hold out VALIDATION_MONTHS (plus the same
# again to fit on) uses these instead of a validation choice.
FALLBACK_LAMBDA = 1e4
FALLBACK_TREES = 100


@dataclass
class Moments:
    """Per-month sufficient statistics for least squares, with an intercept."""

    xtx: np.ndarray  # months x (F+1) x (F+1)
    xty: np.ndarray  # months x (F+1)
    yty: np.ndarray  # months
    n: np.ndarray  # months

    @classmethod
    def empty(cls, n_months: int, n_features: int) -> Moments:
        k = n_features + 1
        return cls(
            np.zeros((n_months, k, k)), np.zeros((n_months, k)), np.zeros(n_months), np.zeros(n_months, dtype=np.int64),
        )

    def add(self, chunk: Chunk) -> None:
        design = _with_intercept(chunk.x)
        y = chunk.y.astype(np.float64)
        for m in np.unique(chunk.month):
            rows = chunk.month == m
            d, t = design[rows], y[rows]
            self.xtx[m] += d.T @ d
            self.xty[m] += d.T @ t
            self.yty[m] += t @ t
            self.n[m] += int(rows.sum())


@dataclass
class Sample:
    """A fixed-seed uniform row sample, for the boosted trees."""

    fraction: float
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(SEED))
    _x: list[np.ndarray] = field(default_factory=list)
    _y: list[np.ndarray] = field(default_factory=list)
    _month: list[np.ndarray] = field(default_factory=list)

    @classmethod
    def for_rows(cls, total_rows: int) -> Sample:
        return cls(fraction=min(1.0, GBM_MAX_ROWS / max(total_rows, 1)))

    def add(self, chunk: Chunk) -> None:
        keep = self.rng.random(len(chunk.y)) < self.fraction
        self._x.append(chunk.x[keep])
        self._y.append(chunk.y[keep])
        self._month.append(chunk.month[keep])

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if not self._x:
            return np.empty((0, 0), np.float32), np.empty(0, np.float32), np.empty(0, np.int32)
        return np.concatenate(self._x), np.concatenate(self._y), np.concatenate(self._month)


def _with_intercept(x: np.ndarray) -> np.ndarray:
    return np.hstack([np.ones((len(x), 1)), x.astype(np.float64)])


def split_validation(train_months: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """(fit months, validation months): the last VALIDATION_MONTHS, purged.

    None when the window is shorter than twice the validation block.
    """
    if len(train_months) < 2 * VALIDATION_MONTHS:
        return None
    validation = train_months[-VALIDATION_MONTHS:]
    fit = train_months[train_months < validation[0] - PURGE_MONTHS]
    return fit, validation


def _solve(moments: Moments, months: np.ndarray, lam: float) -> np.ndarray:
    xtx = moments.xtx[months].sum(axis=0)
    penalty = lam * np.eye(len(xtx))
    penalty[0, 0] = 0.0  # never shrink the intercept
    return np.linalg.solve(xtx + penalty, moments.xty[months].sum(axis=0))


def _sse(moments: Moments, months: np.ndarray, coef: np.ndarray) -> float:
    xtx, xty = moments.xtx[months].sum(axis=0), moments.xty[months].sum(axis=0)
    return float(moments.yty[months].sum() - 2 * coef @ xty + coef @ xtx @ coef)


@dataclass(frozen=True)
class RidgeFit:
    coef: np.ndarray  # intercept first
    lam: float

    def predict(self, x: np.ndarray) -> np.ndarray:
        return _with_intercept(x) @ self.coef


def fit_ridge(moments: Moments, train_months: np.ndarray) -> RidgeFit:
    split = split_validation(train_months)
    lam = FALLBACK_LAMBDA
    if split is not None:
        fit, validation = split
        errors = [_sse(moments, validation, _solve(moments, fit, lam_)) for lam_ in RIDGE_LAMBDAS]
        lam = RIDGE_LAMBDAS[int(np.argmin(errors))]
    return RidgeFit(_solve(moments, train_months, lam), lam)


@dataclass(frozen=True)
class GbmFit:
    model: lgb.LGBMRegressor
    trees: int

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.model.booster_.predict(x, num_iteration=self.trees), dtype=np.float64)


def fit_gbm(sample: tuple[np.ndarray, np.ndarray, np.ndarray], train_months: np.ndarray) -> GbmFit | None:
    """Boosted trees on the sampled rows of the training months.

    Early-stopped on the validation months when the window allows one;
    otherwise a fixed ``FALLBACK_TREES``. None when no sampled row is in the
    window.
    """
    x, y, month = sample
    split = split_validation(train_months)
    fit_months = split[0] if split else train_months
    rows = np.isin(month, fit_months)
    if not rows.any():
        return None
    model = lgb.LGBMRegressor(
        n_estimators=GBM_MAX_TREES if split else FALLBACK_TREES,
        random_state=SEED, n_jobs=GBM_THREADS, deterministic=True, force_row_wise=True,
        **GBM_PARAMS,  # type: ignore[arg-type]
    )
    if split is not None and np.isin(month, split[1]).any():
        held = np.isin(month, split[1])
        model.fit(
            x[rows], y[rows], eval_set=[(x[held], y[held])],
            callbacks=[lgb.early_stopping(GBM_EARLY_STOPPING, verbose=False)],
        )
        trees = int(model.best_iteration_ or GBM_MAX_TREES)
    else:
        model.fit(x[rows], y[rows])
        trees = int(model.n_estimators)
    return GbmFit(model, trees)
