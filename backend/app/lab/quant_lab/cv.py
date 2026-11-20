"""Purge + embargo walk-forward cross-validation splits (AFML ch. 7).

ADR 0015: at the time this module was written, ``quant_ml/pipelines.py::
walk_forward_cv`` only purged (skfolio's ``WalkForward(purged_size=...)``
has no embargo parameter) -- it has since moved to skfolio's
``CombinatorialPurgedCV``, which purges *and* embargoes natively. A real
purge *and* embargo implementation also already existed at
``app.lab.alphacrafter.tuning::walk_forward_splits`` -- but ``alphacrafter``
is a decision-loop package and this is a foundation-tier package, so
importing it would invert the intended dependency direction. The algorithm
is reimplemented standalone here instead of being duplicated by copying
intent alone; see that module for the original.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class LabFold:
    """One expanding-window fold over positional row indices."""

    fold: int
    train_positions: np.ndarray
    test_positions: np.ndarray


def purged_embargo_splits(
    panel: pd.DataFrame | int,
    *,
    n_folds: int = 5,
    purge_days: int = 5,
    embargo_frac: float = 0.01,
) -> list[LabFold]:
    """Chronological expanding-window splits with purge and embargo gaps.

    Args:
        panel: Panel DataFrame (rows are chronological observations) or a row count.
        n_folds: Number of contiguous test blocks; must be >= 5.
        purge_days: Rows dropped between each train prefix and its test block
            (>= forward-return horizon so labels never overlap the train set).
        embargo_frac: Fraction of the sample embargoed after each test block;
            those rows may never re-enter any later fold's training data.

    Returns:
        Folds in strict chronological order; test blocks are disjoint and
        cover the sample, each training set is a purge/embargo-filtered
        prefix ending before its test block.
    """
    n = panel if isinstance(panel, int) else len(panel)
    if n_folds < 5:
        raise ValueError(f"purged_embargo_splits requires n_folds >= 5, got {n_folds}")
    # One extra leading block seeds training so even fold 0 has history.
    total_blocks = n_folds + 1
    if n < total_blocks * max(purge_days + 1, 10):
        raise ValueError(f"panel too small ({n} rows) for {n_folds}-fold walk-forward")

    embargo_days = max(1, int(round(embargo_frac * n)))
    bounds = [round(i * n / total_blocks) for i in range(total_blocks + 1)]

    folds: list[LabFold] = []
    embargo_mask = np.zeros(n, dtype=bool)
    for i in range(n_folds):
        test_start, test_end = bounds[i + 1], bounds[i + 2]
        train_limit = max(test_start - purge_days, 0)
        positions = np.arange(train_limit)
        folds.append(
            LabFold(
                fold=i,
                train_positions=positions[~embargo_mask[:train_limit]],
                test_positions=np.arange(test_start, test_end),
            )
        )
        # Embargo applies to LATER folds only (AFML ch. 7): rows right after a
        # test block carry leaked label information for subsequent training.
        embargo_mask[test_end:test_end + embargo_days] = True
    return folds
