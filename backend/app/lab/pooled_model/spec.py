"""The pre-registered model set and evaluation rules (report Phase 3, step 2).

Everything here is fixed *before* any result is seen. The two models are mined
signals, so under the two-tier evidence standard they carry no money until
they pass DSR/PBO (step 4); this step only produces their honest out-of-sample
record. Changing a constant here after a run is a new trial, not a correction.
"""
from __future__ import annotations

from app.foundation.data_engineering.wrds_factors_schema import JKP_CHARACTERISTICS

# Every JKP characteristic the loader keeps. A column the extract lacks is null
# and so ranks to 0 everywhere, which leaves the models unaffected by it.
FEATURES: tuple[str, ...] = JKP_CHARACTERISTICS

# The documented-premium baseline the models must beat: the value + momentum
# composite from ``factor_premia``, scored on the same rows and months.
BASELINE_FEATURES: tuple[str, ...] = ("be_me", "mom_12_1")

MODELS: tuple[str, ...] = ("ridge", "gbm")
BASELINE = "value_momentum"

# Walk-forward CV over months (``quant_lab.purged_embargo_splits``): 15 test
# blocks of about four years, each model refitted per block on every earlier
# month. The label is a one-month return, so one month of purge and one of
# embargo is enough to keep labels out of the training set.
N_FOLDS = 15
PURGE_MONTHS = 1
EMBARGO_MONTHS = 1

# Ridge: the penalty is chosen per fold on the last VALIDATION_MONTHS of the
# training window (after a purge), by squared error, then refitted on the
# whole window. Features are centred ranks, so X'X's diagonal is about n/12.
RIDGE_LAMBDAS: tuple[float, ...] = (1e0, 1e2, 1e4, 1e6, 1e8)
VALIDATION_MONTHS = 60

# Gradient boosting (LightGBM), trained on a fixed-seed row sample of the
# training window; early stopping on the same validation months.
GBM_MAX_ROWS = 500_000
GBM_PARAMS: dict[str, float | int | str] = {
    "objective": "regression",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 1000,
    "subsample": 0.5,
    "subsample_freq": 1,
    "colsample_bytree": 0.7,
    "reg_lambda": 1.0,
    "verbose": -1,
}
GBM_MAX_TREES = 300
GBM_EARLY_STOPPING = 30
SEED = 20260925

# Reporting: Newey-West lags for monthly series, and the recent window shown
# next to the full out-of-sample record.
NW_LAGS = 6
RECENT_YEARS = 10
