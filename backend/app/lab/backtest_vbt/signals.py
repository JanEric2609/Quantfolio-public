"""Fittable trading signals for walk-forward validation (proposal P3).

The walk-forward validator needs a strategy whose parameters are *fit on the
training window* and then *applied to the test window* — otherwise the
out-of-sample leg is just buy-and-hold and validates nothing.

``SmaCrossoverSignalGenerator`` is a minimal, honest example: a dual moving
average crossover. ``fit`` searches a small (fast, slow) grid on the training
window and keeps the pair with the best in-sample return; ``generate`` turns a
fitted (fast, slow) pair into boolean entry/exit signals on any window.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class FittedParams:
    """Parameters fit on a training window."""

    fast: int
    slow: int


class SmaCrossoverSignalGenerator:
    """Dual simple-moving-average crossover signal generator."""

    def __init__(
        self,
        fast_grid: tuple[int, ...] = (5, 10, 20),
        slow_grid: tuple[int, ...] = (50, 100, 200),
    ) -> None:
        self.fast_grid = fast_grid
        self.slow_grid = slow_grid

    def _position(self, close: pd.Series, fast: int, slow: int) -> pd.Series:
        """Boolean long/flat position: long while fast SMA is above slow SMA."""
        fast_ma = close.rolling(fast, min_periods=fast).mean()
        slow_ma = close.rolling(slow, min_periods=slow).mean()
        # NaN comparisons already yield False, so the result is clean bool.
        return fast_ma > slow_ma

    def _score(self, close: pd.Series, fast: int, slow: int) -> float:
        """In-sample total return of the long/flat crossover strategy."""
        pos = self._position(close, fast, slow)
        daily_ret = close.pct_change().fillna(0.0)
        # Trade on the prior day's signal to avoid look-ahead.
        strat_ret = daily_ret * pos.shift(1, fill_value=False).astype(float)
        return float(np.prod(1.0 + strat_ret.to_numpy()) - 1.0)

    def fit(self, train_df: pd.DataFrame) -> FittedParams:
        """Pick the (fast, slow) pair with the best in-sample return on train."""
        close = train_df.iloc[:, 0] if train_df.shape[1] >= 1 else pd.Series(dtype=float)
        n = len(close)
        best: FittedParams | None = None
        best_score = -np.inf
        for fast in self.fast_grid:
            for slow in self.slow_grid:
                if fast >= slow or slow >= n:
                    continue
                score = self._score(close, fast, slow)
                if score > best_score:
                    best_score = score
                    best = FittedParams(fast=fast, slow=slow)
        if best is None:
            # Window too short for the grid; fall back to the smallest valid pair.
            fast = min(self.fast_grid)
            slow = max(min(self.slow_grid), fast + 1)
            best = FittedParams(fast=fast, slow=slow)
        return best

    def generate(
        self, prices_df: pd.DataFrame, params: FittedParams
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Produce boolean entry/exit signals for each symbol column.

        Entry = the day the position flips flat→long (fast crosses above slow);
        exit = the day it flips long→flat. Signals are aligned to *prices_df*.
        """
        entries = pd.DataFrame(False, index=prices_df.index, columns=prices_df.columns)
        exits = pd.DataFrame(False, index=prices_df.index, columns=prices_df.columns)
        for col in prices_df.columns:
            pos = self._position(cast(pd.Series, prices_df[col]), params.fast, params.slow)
            prev = pos.shift(1, fill_value=False)
            entries[col] = pos & ~prev
            exits[col] = ~pos & prev
        return entries.astype(bool), exits.astype(bool)
