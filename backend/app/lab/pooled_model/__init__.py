"""Pooled cross-sectional model on the JKP panel (report Phase 3, step 2).

Ridge and gradient boosting over all JKP characteristics, ranked within
country and month, fitted under purged walk-forward CV and graded out of
sample against the value + momentum baseline. See ``CONTEXT.md``.
"""

from app.lab.pooled_model.spec import BASELINE, FEATURES, MODELS
from app.lab.pooled_model.study import (
    TRIAL_CONTEXT,
    ModelCard,
    ModelStudy,
    month_series,
    results_path,
    run_model_study,
    scores_path,
)

__all__ = [
    "BASELINE",
    "FEATURES",
    "MODELS",
    "TRIAL_CONTEXT",
    "ModelCard",
    "ModelStudy",
    "month_series",
    "results_path",
    "run_model_study",
    "scores_path",
]
