"""Shared ML-model training core (Track D1b) — used by both the /ml/train API
endpoint (indirectly, via the same feature/label prep shape) and the
scheduled discover-ml-training job (directly), so there is one place that
turns (prices, dates) into an aligned (X, y) ready for walk_forward_cv,
rather than two copies of the same pipeline drifting apart.

Deliberately does NOT fetch price history itself (no app.foundation.market
import): quant_ml is foundation-tier, and a quant_ml -> market edge merges
into an existing dense import-cycle SCC (advisor/discover/... already
cyclically tied to market) — confirmed via check_no_new_cycles.py, same
issue build_features' regime wiring hit. Callers (already sitting above
quant_ml, e.g. the API layer or discover, which already imports market)
fetch prices/dates themselves and pass them in.

The same holds for app.foundation.data_engineering.pit_panel_joins (Phase 5's
WRDS/IBES/insider PIT-join helpers) even though app.lab.alphacrafter.panel
already imports it cleanly — that module isn't the thing lab.regime reaches
back into. Confirmed via check_no_new_cycles.py: app.lab.quant_ml importing
pit_panel_joins directly closes a cycle through the existing seeded exception
foundation -> lab.regime (lazy) -> lab.quant_ml.registry (module-level) —
same failure shape as the market case, different module. So this file stays
just as data-fetching-free for Phase 5's PIT signal as it already is for
market: prepare_training_data/train_and_validate_ticker_model take an
extra_features param and thread it into build_features, but never fetch it
themselves. The caller (discover/jobs.py, decision-tier, already imports
pit_panel_joins directly with no such issue) fetches it and passes it in.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)

ModelKind = Literal["lgbm", "xgb", "mlp"]

# Bumped whenever build_features' output column set changes in a way that
# invalidates already-persisted QuantMlModel artefacts (predict_pipeline is
# strict about matching the fitted pipeline's feature count/order). Readers
# of a persisted model (stage_ml_signal, /ml/models/{id}/predict) must treat
# a row whose feature_schema_version doesn't match this as equivalent to "no
# model" rather than attempting to predict with it. See Phase 5 plan.
CURRENT_FEATURE_SCHEMA_VERSION = 1


def prepare_training_data(
    prices: list[float],
    dates: list[str],
    *,
    label_method: Literal["fixed_horizon", "triple_barrier"] = "triple_barrier",
    horizon: int = 20,
    upper_barrier: float = 0.02,
    lower_barrier: float = 0.02,
    extra_features: Any | None = None,
) -> tuple[Any, Any] | None:
    """Build an aligned (X, y) from a price series.

    Returns ``None`` when there isn't enough history (<60 days) or the
    aligned result is too thin (<30 rows) to build a meaningful
    feature/label pair — callers should treat that as a skip, not an error.

    ``extra_features``, when given, is passed straight through to
    ``build_features`` — see its docstring for the per-row join contract.
    """
    import pandas as pd

    from app.lab.quant_ml.features import build_features
    from app.lab.quant_ml.labels import fixed_horizon_labels, triple_barrier_labels

    if len(prices) < 60:
        return None

    X_df = build_features(prices, dates, extra_features=extra_features)
    if label_method == "triple_barrier":
        labels_raw = triple_barrier_labels(
            prices, upper_barrier=upper_barrier, lower_barrier=lower_barrier, time_horizon=horizon,
        )
    else:
        labels_raw = fixed_horizon_labels(prices, horizon=horizon)

    labels_index = pd.to_datetime(dates[: len(labels_raw)])
    y_raw = pd.Series(labels_raw, index=labels_index)
    y = y_raw.reindex(X_df.index)
    mask = y.notna()
    y = y[mask].astype(int)
    X_aligned = X_df.loc[mask]
    if len(X_aligned) < 30:
        return None
    return X_aligned, y


def train_and_validate_ticker_model(
    db: Any,
    user_id: str,
    ticker: str,
    prices: list[float],
    dates: list[str],
    *,
    kind: ModelKind = "lgbm",
    label_method: Literal["fixed_horizon", "triple_barrier"] = "triple_barrier",
    horizon: int = 20,
    model_dir: str | None = None,
    extra_features: Any | None = None,
) -> Any:
    """Train a fresh model for *ticker* from already-fetched *prices*/*dates*,
    gated by walk_forward_cv's majority-class-baseline check (Track B0), and
    persist it as a ``QuantMlModel`` row (``ticker`` set, so discover's
    stage_ml_signal can later find it). Always persisted — status reflects
    whether it passed the gate (``"completed"``) or not
    (``"rejected_below_baseline"`` etc.), matching the existing /ml/train
    endpoint's discipline: an unvalidated model is never silently promoted,
    but the attempt is still recorded.

    *extra_features*, when given, is threaded straight into ``build_features``
    (Phase 5's WRDS/IBES/insider point-in-time signal — see
    ``pit_panel_joins.pit_ml_features_for_symbol``). Deliberately NOT fetched
    in here: this module never reaches into ``app.foundation.data_engineering``
    itself (same reasoning as the "no app.foundation.market" rule in this
    module's docstring — confirmed via check_no_new_cycles.py that this
    specific edge closes a cycle through the existing
    ``foundation -> lab.regime -> quant_ml.registry`` seeded exception,
    the same failure shape the market-import avoidance already existed
    for). The caller (``discover/jobs.py``, decision-tier, already imports
    ``pit_panel_joins`` directly with no such issue) fetches it and passes
    it in, exactly like *prices*/*dates* already work.

    Returns the persisted ``QuantMlModel`` row, or ``None`` if there wasn't
    enough price history to even attempt training.
    """
    from datetime import UTC, datetime

    from app.foundation.models.entities import QuantMlModel
    from app.lab.quant_ml.pipelines import build_pipeline, walk_forward_cv
    from app.lab.quant_ml.registry import save_model

    prepared = prepare_training_data(
        prices, dates, label_method=label_method, horizon=horizon, extra_features=extra_features,
    )
    if prepared is None:
        logger.info("discover_ml_training: insufficient data for %s, skipping", ticker)
        return None
    X_aligned, y = prepared

    row = QuantMlModel(
        user_id=user_id,
        name=f"discover-ml-{ticker}",
        kind=kind,
        ticker=ticker.upper(),
        status="training",
        feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    pipeline = build_pipeline(kind=cast(ModelKind, kind), params={})
    cv_result = walk_forward_cv(pipeline, X_aligned, y)
    metrics = {k: v for k, v in cv_result.items() if k != "fitted_pipeline"}
    fitted = cv_result.get("fitted_pipeline")

    if cv_result.get("status") != "completed":
        row.metrics_json = json.dumps(metrics)
        row.status = str(cv_result.get("status", "failed"))
        row.error_message = cv_result.get("message")
        row.finished_at = datetime.now(UTC)
        db.commit()
        return row

    artefact_path = (
        save_model(fitted, row.id, base=model_dir) if model_dir else save_model(fitted, row.id)
    )
    row.artefact_path = artefact_path
    row.metrics_json = json.dumps(metrics)
    row.status = "completed"
    row.finished_at = datetime.now(UTC)
    db.commit()
    return row
