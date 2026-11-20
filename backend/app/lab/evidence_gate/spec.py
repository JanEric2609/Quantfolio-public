"""The step-4 pass rule (report Phase 3), fixed before any result is seen.

A mined signal may touch money only if its record survives both tests of
Bailey & López de Prado that the report's two-tier evidence standard names:

- the Deflated Sharpe Ratio, against the expected best of every trial ever
  recorded on the global ledger (``resolve_n_trials``, the real count);
- the Probability of Backtest Overfitting (CSCV) over the pre-registered
  family the candidate was chosen from.
"""
from __future__ import annotations

# DSR is a probability that the true Sharpe beats the best of n null trials.
DSR_PASS = 0.95
# PBO is the share of CSCV splits in which the in-sample winner lands below
# the out-of-sample median; above one half, picking the best is worse than
# picking at random.
PBO_MAX = 0.5
# 16 contiguous blocks: C(16, 8) = 12,870 splits, and blocks of about two
# years on the JKP record.
PBO_PARTITIONS = 16
PERIODS_PER_YEAR = 12
NW_LAGS = 6
