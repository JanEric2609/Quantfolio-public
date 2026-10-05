"""sklearn-style ML pipelines for return prediction.

Supports LightGBM, XGBoost, and PyTorch MLP via a unified interface.
Out-of-sample validation uses skfolio's Combinatorial Purged Cross-Validation
(CPCV, Lopez de Prado) rather than a fixed-window walk-forward split, since the
latter's hard train-window-size cliff made it fail outright on the sample
sizes this app's tickers actually produce.
"""
from __future__ import annotations

from typing import Any, Literal

ModelKind = Literal["lgbm", "xgb", "mlp"]


def build_pipeline(
    kind: ModelKind = "lgbm",
    params: dict | None = None,
) -> Any:
    """Build a sklearn-compatible pipeline for the given estimator kind."""
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    params = params or {}

    if kind == "lgbm":
        from lightgbm import LGBMClassifier
        est = LGBMClassifier(
            n_estimators=params.get("n_estimators", 100),
            learning_rate=params.get("learning_rate", 0.05),
            num_leaves=params.get("num_leaves", 31),
            random_state=42,
            verbose=-1,
        )
    elif kind == "xgb":
        from xgboost import XGBClassifier
        est = XGBClassifier(
            n_estimators=params.get("n_estimators", 100),
            learning_rate=params.get("learning_rate", 0.05),
            max_depth=params.get("max_depth", 4),
            use_label_encoder=False,
            eval_metric="logloss",
            random_state=42,
            verbosity=0,
        )
    elif kind == "mlp":
        from sklearn.neural_network import MLPClassifier
        est = MLPClassifier(
            hidden_layer_sizes=params.get("hidden_layers", (64, 32)),
            max_iter=params.get("max_iter", 200),
            random_state=42,
        )
    else:
        raise ValueError(f"Unknown model kind: {kind}")

    # constant/0.0 (not "median") deliberately sidesteps the degenerate case
    # of an all-NaN training column (e.g. a ticker with zero WRDS/IBES/
    # insider coverage ever), where a median imputer would itself compute
    # NaN. 0.0 is a defensible neutral value for every PIT-signal column
    # build_features' extra_features can carry (already winsorized/z-scored
    # WRDS characteristics, naturally-zero insider flow/cluster score, and
    # 0/1 availability flags). A no-op for every pre-existing caller, since
    # none of today's technical/calendar features carry NaN by the time
    # they reach here (build_features already drops those rows).
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="constant", fill_value=0.0)),
            ("scaler", StandardScaler()),
            ("model", est),
        ]
    )


def train_pipeline(
    pipeline: Any,
    X: Any,
    y: Any,
    holdout_ratio: float = 0.2,
) -> dict[str, Any]:
    """Fit a pipeline and return training + holdout metrics.

    Uses a temporal split: first ``(1 - holdout_ratio)`` rows for training,
    last ``holdout_ratio`` rows for holdout evaluation.  This avoids
    inflated in-sample metrics.
    """
    from sklearn.metrics import accuracy_score, f1_score

    n = len(y)
    split = max(1, int(n * (1 - holdout_ratio)))
    # Simple embargo margin before the holdout set to prevent leakage
    train_end = max(1, split - 5)

    X_train, X_holdout = X.iloc[:train_end], X.iloc[split:]
    y_train, y_holdout = y.iloc[:train_end], y.iloc[split:]

    pipeline.fit(X_train, y_train)

    train_preds = pipeline.predict(X_train)
    train_acc = float(accuracy_score(y_train, train_preds))
    train_f1 = float(f1_score(y_train, train_preds, average="weighted", zero_division="warn"))

    result: dict[str, Any] = {
        "train_accuracy": train_acc,
        "train_f1": train_f1,
        "n_samples": len(y),
        "n_features": X.shape[1],
    }

    if len(y_holdout) > 0:
        holdout_preds = pipeline.predict(X_holdout)
        result["holdout_accuracy"] = float(accuracy_score(y_holdout, holdout_preds))
        result["holdout_f1"] = float(
            f1_score(y_holdout, holdout_preds, average="weighted", zero_division="warn")
        )
        result["holdout_samples"] = len(y_holdout)
    else:
        result["holdout_accuracy"] = None
        result["holdout_f1"] = None
        result["holdout_samples"] = 0

    return result


def predict_pipeline(pipeline: Any, X: Any) -> list[int]:
    return [int(p) for p in pipeline.predict(X)]


def _majority_class_baseline(y: Any) -> float:
    """Accuracy of always predicting the most frequent class in *y*."""
    counts = y.value_counts()
    if counts.empty:
        return 0.0
    return float(counts.max() / counts.sum())


def _pick_cpcv_params(
    n_samples: int,
    n_folds: int,
    n_test_folds: int,
    purged_size: int,
    embargo_size: int,
) -> tuple[int, int, int, int] | None:
    """Choose feasible CPCV parameters for ``n_samples`` observations.

    ``CombinatorialPurgedCV`` requires ``purged_size + embargo_size <
    (n_samples // n_folds) - 1``. Tries ``n_folds`` first, then falls back to
    4 and 3 folds (trimming ``purged_size``/``embargo_size`` as needed) so a
    ticker with a thin-but-valid sample degrades gracefully instead of
    raising. Returns ``None`` if no candidate fold count fits.
    """
    for nf in (n_folds, 4, 3):
        if nf < 2 or nf > n_samples:
            continue
        nt = min(n_test_folds, nf - 1)
        if nt < 1:
            continue
        min_fold_size = n_samples // nf
        max_pe = min_fold_size - 2
        if max_pe < 0:
            continue
        p = max(0, min(purged_size, max_pe))
        e = max(0, min(embargo_size, max_pe - p))
        return nf, nt, p, e
    return None


def walk_forward_cv(
    pipeline_factory: Any,
    X: Any,
    y: Any,
    n_folds: int = 6,
    n_test_folds: int = 2,
    purged_size: int = 20,
    embargo_size: int = 5,
    baseline_margin: float = 0.05,
) -> dict[str, Any]:
    """Combinatorial Purged Cross-Validation (Lopez de Prado) OOS evaluation.

    Returns metrics AND the last fitted pipeline clone so the caller can
    persist a usable artefact (the original ``pipeline_factory`` stays
    unfitted). A model whose OOS accuracy doesn't clear the naive
    majority-class baseline (computed from the same aligned ``y``) by at
    least ``baseline_margin`` is rejected — ``fitted_pipeline`` is withheld
    (``None``) so a caller that blindly persists it on any non-``None``
    check doesn't ship an unvalidated model.

    ``purged_size`` should match the label horizon (rows whose forward-return
    window overlaps the test fold leak information) and ``embargo_size`` the
    feature lag warmup. Fold counts shrink automatically (6 -> 4 -> 3,
    trimming purge/embargo) for thinner samples rather than raising.
    """
    try:
        from skfolio.model_selection import CombinatorialPurgedCV
        from sklearn.base import clone
        from sklearn.metrics import accuracy_score

        n_samples = len(X)
        params = _pick_cpcv_params(n_samples, n_folds, n_test_folds, purged_size, embargo_size)
        if params is None:
            return {
                "status": "unavailable",
                "message": f"Not enough observations ({n_samples}) for CPCV.",
                "fitted_pipeline": None,
            }
        nf, nt, p, e = params
        splitter = CombinatorialPurgedCV(n_folds=nf, n_test_folds=nt, purged_size=p, embargo_size=e)

        all_preds: list[int] = []
        all_true: list[int] = []
        last_fitted = None

        for train_index, test_indices in splitter.split(X):
            X_train, y_train = X.iloc[train_index], y.iloc[train_index]
            pipe: Any = clone(pipeline_factory)
            pipe.fit(X_train, y_train)
            for test_index in test_indices:
                preds = pipe.predict(X.iloc[test_index])
                all_preds.extend(preds.tolist())
                all_true.extend(y.iloc[test_index].tolist())
            last_fitted = pipe

        if not all_preds:
            return {
                "status": "unavailable",
                "message": "No CPCV splits generated.",
                "fitted_pipeline": None,
            }

        acc = float(accuracy_score(all_true, all_preds))
        baseline_acc = _majority_class_baseline(y)
        n_folds_actual = splitter.n_splits
        if acc < baseline_acc + baseline_margin:
            return {
                "status": "rejected_below_baseline",
                "message": (
                    f"OOS accuracy {acc:.4f} did not clear majority-class baseline "
                    f"{baseline_acc:.4f} + margin {baseline_margin:.4f}."
                ),
                "n_folds": n_folds_actual,
                "oos_accuracy": acc,
                "baseline_accuracy": baseline_acc,
                "n_oos_samples": len(all_preds),
                "fitted_pipeline": None,
            }
        return {
            "status": "completed",
            "n_folds": n_folds_actual,
            "oos_accuracy": acc,
            "baseline_accuracy": baseline_acc,
            "n_oos_samples": len(all_preds),
            "fitted_pipeline": last_fitted,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "message": str(exc),
            "fitted_pipeline": None,
        }


def write_signal_rows(
    db: Any,
    run_id: str,
    symbol: str,
    dates: list[str],
    predictions: list[int],
) -> None:
    """Persist inference output to QuantSignal rows."""
    from app.foundation.models.entities import QuantSignal
    from datetime import date

    for dt_str, pred in zip(dates, predictions):
        try:
            as_of = date.fromisoformat(str(dt_str)[:10])
        except Exception:
            continue
        row = QuantSignal(
            run_id=run_id,
            symbol=symbol,
            signal_name="ml_direction",
            signal_value=pred,
            as_of_date=as_of,
        )
        db.add(row)
    db.commit()
